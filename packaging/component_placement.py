"""Trusted host-role placement and peer evidence for native component groups.

This module does not discover or assert local updater, service, or runtime state.
It validates the signed placement policy, root-owned host topology, and responses
from the fixed peer helper over pinned SSH.
此模块不采集或虚构本机运行状态；只校验受信放置策略、root 主机配置及固定 peer helper 回执。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import secrets
import selectors
import stat
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/cyrene/component-deployment.json")
SSH_EXECUTABLE = "/usr/bin/ssh"
SUDO_EXECUTABLE = "/usr/bin/sudo"
PEER_HELPER = "/usr/libexec/cyrene-component-update-helper"
MAX_CONFIG_BYTES = 64 * 1024
MAX_PEER_RESPONSE_BYTES = 64 * 1024
MAX_PEER_STDERR_BYTES = 8 * 1024
MAX_PEER_TTL_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 5
DEFAULT_SSH_TIMEOUT_SECONDS = 15

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SSH_HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HOSTNAME_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_PLAN_ID_RE = re.compile(r"^plan-[0-9a-f]{32}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PHASES = frozenset({"ABSENT", "STAGED", "PREPARED_HELD", "ACTIVE_HELD", "ACTIVE"})
_OPERATIONS = frozenset({"check", "stage", "apply"})


class PlacementError(ValueError):
    """Raised when trusted placement inputs or peer evidence fail closed."""


@dataclass(frozen=True)
class DeploymentRole:
    """One signed host-role member policy from a compatibility group."""

    role_id: str
    required_members: frozenset[str]
    allowed_members: frozenset[str]


@dataclass(frozen=True)
class PeerConfig:
    """Pinned SSH identity and helper destination; private key bytes stay unread."""

    host_id: str
    role_id: str
    host_name: str
    ssh_user: str
    ssh_port: int
    identity_file: Path
    known_hosts_file: Path
    known_hosts_sha256: str
    host_key_fingerprint: str


@dataclass(frozen=True)
class PlacementContext:
    """Validated root-owned local placement and signed two-role group policy."""

    deployment_id: str
    workspace_identity: dict[str, str]
    host_id: str
    role_id: str
    peer: PeerConfig
    catalog_digest: str
    group_id: str
    group_version: str
    contract_lock: dict[str, Any]
    topology_digest: str
    local_config_digest: str
    roles: dict[str, DeploymentRole]

    @property
    def local_role(self) -> DeploymentRole:
        return self.roles[self.role_id]

    @property
    def peer_role(self) -> DeploymentRole:
        return self.roles[self.peer.role_id]


@dataclass(frozen=True)
class PlacementSelection:
    """Role-local compatibility candidates and deliberately excluded peers."""

    component_ids: tuple[str, ...]
    excluded_other_role_ids: tuple[str, ...]


@dataclass(frozen=True)
class PeerEvidence:
    """Validated fixed-helper response with stable and freshness-bound digests."""

    payload: dict[str, Any]
    stable_digest: str
    response_digest: str

    @property
    def phase(self) -> str:
        return str(self.payload["phase"])


@dataclass(frozen=True)
class PeerPlanEvidence:
    """Read-only verified peer target plan; it conveys no readiness or authority."""

    payload: dict[str, Any]
    stable_digest: str
    response_digest: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlacementError("duplicate JSON object key")
        result[key] = value
    return result


def _read_json_object(
    path: Path, *, expected_uid: int, max_bytes: int, private: bool
) -> dict[str, Any]:
    """Read one no-follow root-owned JSON file after validating its path chain."""

    _verify_path_chain(path.parent, expected_uid=expected_uid)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PlacementError("trusted JSON file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, expected_uid}
            or info.st_nlink != 1
            or (private and stat.S_IMODE(info.st_mode) & 0o077)
            or (not private and stat.S_IMODE(info.st_mode) & 0o022)
        ):
            raise PlacementError("trusted JSON file ownership or mode is unsafe")
        if info.st_size < 2 or info.st_size > max_bytes:
            raise PlacementError("trusted JSON file size is invalid")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise PlacementError("trusted JSON file exceeds its size limit")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlacementError("trusted JSON file is malformed") from exc
    if not isinstance(value, dict):
        raise PlacementError("trusted JSON file must contain an object")
    return value


def _verify_path_chain(directory: Path, *, expected_uid: int) -> None:
    """Reject symlinked, non-root-owned, or writable ancestors of trusted files."""

    absolute = directory.absolute()
    if ".." in absolute.parts:
        raise PlacementError("trusted path contains parent traversal")
    for current in (absolute, *absolute.parents):
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise PlacementError("trusted path ancestor is unavailable") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, expected_uid}
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise PlacementError("trusted path ancestor ownership or mode is unsafe")
        if current == Path("/"):
            break


def _validate_trusted_leaf(
    path: Path,
    *,
    expected_uid: int,
    private: bool,
    exact_mode: int | None = None,
) -> None:
    """Check a configured SSH file without opening private key contents."""

    _verify_path_chain(path.parent, expected_uid=expected_uid)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PlacementError("configured SSH file is unavailable") from exc
    mode = stat.S_IMODE(info.st_mode)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != expected_uid
        or info.st_nlink != 1
        or (private and mode != 0o600)
        or (not private and mode & 0o022)
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise PlacementError("configured SSH file ownership or mode is unsafe")


def _validate_role_policy(group: dict[str, Any]) -> dict[str, DeploymentRole] | None:
    """Parse signed deploymentRoles, preserving legacy behavior when absent."""

    if "deploymentRoles" not in group:
        return None
    raw_roles = group.get("deploymentRoles")
    members = group.get("members")
    if not isinstance(raw_roles, list) or len(raw_roles) != 2 or not isinstance(members, list):
        raise PlacementError("signed deployment role policy is incomplete")
    member_ids = {
        member.get("componentId")
        for member in members
        if isinstance(member, dict) and isinstance(member.get("componentId"), str)
    }
    if len(member_ids) != len(members) or not member_ids:
        raise PlacementError("signed compatibility members are malformed")
    roles: dict[str, DeploymentRole] = {}
    for raw in raw_roles:
        if not isinstance(raw, dict) or set(raw) != {"roleId", "requiredMembers", "allowedMembers"}:
            raise PlacementError("signed deployment role fields are invalid")
        role_id = raw.get("roleId")
        required = raw.get("requiredMembers")
        allowed = raw.get("allowedMembers")
        if (
            not isinstance(role_id, str)
            or not _ID_RE.fullmatch(role_id)
            or role_id in roles
            or not isinstance(required, list)
            or not isinstance(allowed, list)
            or not required
            or any(
                not isinstance(item, str) or not _ID_RE.fullmatch(item)
                for item in [*required, *allowed]
            )
            or len(set(required)) != len(required)
            or len(set(allowed)) != len(allowed)
        ):
            raise PlacementError("signed deployment role members are invalid")
        required_set = frozenset(required)
        allowed_set = frozenset(allowed)
        if not required_set <= allowed_set or not allowed_set <= member_ids:
            raise PlacementError("required deployment members must be allowed group members")
        roles[role_id] = DeploymentRole(role_id, required_set, allowed_set)
    required_by_role = [component for role in roles.values() for component in role.required_members]
    if len(required_by_role) != len(set(required_by_role)):
        raise PlacementError("a required component belongs to multiple deployment roles")
    allowed_by_role = [component for role in roles.values() for component in role.allowed_members]
    if len(allowed_by_role) != len(set(allowed_by_role)) or set(allowed_by_role) != member_ids:
        raise PlacementError("every group member must be allowed by a deployment role")
    if set(roles) != {"control-host", "connector-host"}:
        raise PlacementError("signed deployment policy must define control-host and connector-host")
    return roles


def _valid_identity_field(value: Any, *, max_length: int = 256) -> bool:
    """Match the Authority admission identity rule: bounded, trimmed, no controls."""

    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= max_length
        and value.strip() == value
        and not any(unicodedata.category(char) == "Cc" for char in value)
    )


def load_placement(
    group: dict[str, Any],
    *,
    trusted_catalog_digest: str,
    config_path: Path = DEFAULT_CONFIG_PATH,
    expected_uid: int = 0,
) -> PlacementContext | None:
    """Load fixed host placement for one already-verified signed group.

    Groups without ``deploymentRoles`` return ``None`` so their current updater
    semantics remain unchanged. A valid role policy is checked before reading
    the host config; if that config is absent, ``None`` preserves the explicit
    whole-group-local legacy mode. Once a config exists, invalid or unsafe
    config, SSH pins, or policy bindings fail closed.
    """

    roles = _validate_role_policy(group)
    if roles is None:
        return None
    if not _DIGEST_RE.fullmatch(trusted_catalog_digest):
        raise PlacementError("trusted catalog digest is invalid")
    try:
        os.lstat(config_path)
    except FileNotFoundError:
        # A catalog may be deployed before the host-role rollout. Until root
        # writes the fixed topology file, retain the existing all-local policy.
        ancestor = config_path.parent
        while True:
            try:
                os.lstat(ancestor)
                _verify_path_chain(ancestor, expected_uid=expected_uid)
                break
            except FileNotFoundError:
                if ancestor == ancestor.parent:
                    raise PlacementError("host placement config path has no safe ancestor")
                ancestor = ancestor.parent
        return None
    except OSError as exc:
        raise PlacementError("host placement config cannot be inspected") from exc
    config = _read_json_object(
        config_path,
        expected_uid=expected_uid,
        max_bytes=MAX_CONFIG_BYTES,
        private=True,
    )
    expected_keys = {
        "schemaVersion",
        "deploymentId",
        "hostId",
        "hostName",
        "hostKeyFingerprint",
        "sshPort",
        "roleId",
        "catalogDigest",
        "groupId",
        "workspaceIdentity",
        "peer",
    }
    if set(config) != expected_keys or config.get("schemaVersion") != 1:
        raise PlacementError("host placement config fields are invalid")
    group_id = group.get("groupId")
    group_version = group.get("groupVersion")
    contract_lock = group.get("contractLock")
    if (
        not isinstance(group_id, str)
        or not _ID_RE.fullmatch(group_id)
        or group_id != "workspace-product-v2"
        or not isinstance(group_version, str)
        or not re.fullmatch(r"[1-9][0-9]{0,7}", group_version)
        or not isinstance(contract_lock, dict)
        or config.get("catalogDigest") != trusted_catalog_digest
        or config.get("groupId") != group_id
    ):
        raise PlacementError("host placement config does not bind the trusted catalog group")
    if (
        set(contract_lock) != {"repository", "commit", "path", "sha256"}
        or contract_lock.get("repository") != "DoHorizon-AI/Cyrene-Workspace"
        or not isinstance(contract_lock.get("commit"), str)
        or not _COMMIT_RE.fullmatch(contract_lock["commit"])
        or not isinstance(contract_lock.get("path"), str)
        or contract_lock["path"].startswith("/")
        or ".." in Path(contract_lock["path"]).parts
        or not isinstance(contract_lock.get("sha256"), str)
        or not _DIGEST_RE.fullmatch(contract_lock["sha256"])
    ):
        raise PlacementError("trusted Workspace contract lock fields are invalid")
    deployment_id = config.get("deploymentId")
    host_id = config.get("hostId")
    role_id = config.get("roleId")
    workspace_identity_value = config.get("workspaceIdentity")
    if not isinstance(workspace_identity_value, dict) or set(workspace_identity_value) != {
        "organizationId",
        "workspaceId",
        "authorityInstanceId",
    }:
        raise PlacementError("fixed Workspace identity fields are invalid")
    workspace_identity = {
        key: workspace_identity_value[key]
        for key in ("organizationId", "workspaceId", "authorityInstanceId")
    }
    if not all(_valid_identity_field(value) for value in workspace_identity.values()):
        raise PlacementError("fixed Workspace identity values are invalid")
    if (
        not isinstance(deployment_id, str)
        or not _HOST_ID_RE.fullmatch(deployment_id)
        or not isinstance(host_id, str)
        or not _SSH_HOST_ID_RE.fullmatch(host_id)
        or not isinstance(role_id, str)
        or role_id not in roles
    ):
        raise PlacementError("local deployment identity is invalid")
    peer_value = config.get("peer")
    peer_keys = {
        "hostId",
        "roleId",
        "hostName",
        "sshUser",
        "sshPort",
        "identityFile",
        "knownHostsFile",
        "knownHostsSha256",
        "hostKeyFingerprint",
    }
    if not isinstance(peer_value, dict) or set(peer_value) != peer_keys:
        raise PlacementError("pinned SSH peer fields are invalid")
    peer_host = peer_value.get("hostId")
    peer_role = peer_value.get("roleId")
    peer_name = peer_value.get("hostName")
    user = peer_value.get("sshUser")
    port = peer_value.get("sshPort")
    identity_path = peer_value.get("identityFile")
    known_hosts_path = peer_value.get("knownHostsFile")
    if not isinstance(identity_path, str) or not isinstance(known_hosts_path, str):
        raise PlacementError("SSH identity paths are invalid")
    identity_file = Path(identity_path)
    known_hosts_file = Path(known_hosts_path)
    known_hosts_digest = peer_value.get("knownHostsSha256")
    fingerprint = peer_value.get("hostKeyFingerprint")
    if (
        not isinstance(peer_host, str)
        or not _SSH_HOST_ID_RE.fullmatch(peer_host)
        or peer_host == host_id
        or not isinstance(peer_role, str)
        or peer_role not in roles
        or peer_role == role_id
        or not isinstance(peer_name, str)
        or not _valid_host_name(peer_name)
        or not isinstance(user, str)
        or not _SSH_USER_RE.fullmatch(user)
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not identity_file.is_absolute()
        or not known_hosts_file.is_absolute()
        or not isinstance(known_hosts_digest, str)
        or not _BARE_DIGEST_RE.fullmatch(known_hosts_digest)
        or not isinstance(fingerprint, str)
        or not _FINGERPRINT_RE.fullmatch(fingerprint)
    ):
        raise PlacementError("pinned SSH peer identity is invalid")
    if identity_file == known_hosts_file:
        raise PlacementError("SSH identity and known_hosts paths must be distinct")
    _validate_trusted_leaf(identity_file, expected_uid=expected_uid, private=True, exact_mode=0o600)
    _validate_trusted_leaf(known_hosts_file, expected_uid=expected_uid, private=False)
    known_hosts_bytes = _read_trusted_bytes(
        known_hosts_file,
        expected_uid=expected_uid,
        max_bytes=MAX_CONFIG_BYTES,
        private=False,
    )
    if hashlib.sha256(known_hosts_bytes).hexdigest() != known_hosts_digest:
        raise PlacementError("pinned known_hosts digest does not match the host config")
    _verify_known_host_fingerprint(
        known_hosts_bytes,
        host_id=peer_host,
        expected_fingerprint=fingerprint,
    )
    local_name = config.get("hostName")
    local_fingerprint = config.get("hostKeyFingerprint")
    local_port = config.get("sshPort")
    if (
        not isinstance(local_name, str)
        or not _valid_host_name(local_name)
        or not isinstance(local_fingerprint, str)
        or not _FINGERPRINT_RE.fullmatch(local_fingerprint)
        or not isinstance(local_port, int)
        or isinstance(local_port, bool)
        or not 1 <= local_port <= 65535
    ):
        raise PlacementError("local public SSH identity is invalid")
    peer = PeerConfig(
        host_id=peer_host,
        role_id=peer_role,
        host_name=peer_name,
        ssh_user=user,
        ssh_port=port,
        identity_file=identity_file,
        known_hosts_file=known_hosts_file,
        known_hosts_sha256=known_hosts_digest,
        host_key_fingerprint=fingerprint,
    )
    host_records = sorted(
        [
            {
                "hostId": host_id,
                "roleId": role_id,
                "hostName": local_name,
                "sshPort": local_port,
                "hostKeyFingerprint": local_fingerprint,
            },
            {
                "hostId": peer.host_id,
                "roleId": peer.role_id,
                "hostName": peer.host_name,
                "sshPort": peer.ssh_port,
                "hostKeyFingerprint": peer.host_key_fingerprint,
            },
        ],
        key=lambda item: item["hostId"],
    )
    topology = {
        "deploymentId": deployment_id,
        "workspaceIdentity": workspace_identity,
        "hosts": host_records,
        "catalogDigest": trusted_catalog_digest,
        "groupId": group_id,
        "groupVersion": group_version,
        "contractLock": contract_lock,
        "deploymentRoles": [
            {
                "roleId": item.role_id,
                "requiredMembers": sorted(item.required_members),
                "allowedMembers": sorted(item.allowed_members),
            }
            for item in sorted(roles.values(), key=lambda item: item.role_id)
        ],
    }
    topology_digest = "sha256:" + hashlib.sha256(_canonical_bytes(topology)).hexdigest()
    local_config_digest = "sha256:" + hashlib.sha256(_canonical_bytes(config)).hexdigest()
    return PlacementContext(
        deployment_id=deployment_id,
        workspace_identity=workspace_identity,
        host_id=host_id,
        role_id=role_id,
        peer=peer,
        catalog_digest=trusted_catalog_digest,
        group_id=group_id,
        group_version=group_version,
        contract_lock=contract_lock,
        topology_digest=topology_digest,
        local_config_digest=local_config_digest,
        roles=roles,
    )


def plan_binding(context: PlacementContext) -> dict[str, Any]:
    """Return the exact placement material the updater must include in planDigest."""

    return {
        "deploymentId": context.deployment_id,
        "workspaceIdentity": context.workspace_identity,
        "hostId": context.host_id,
        "roleId": context.role_id,
        "topologyDigest": context.topology_digest,
        "localConfigDigest": context.local_config_digest,
        "catalogDigest": context.catalog_digest,
        "groupId": context.group_id,
        "groupVersion": context.group_version,
        "contractLock": context.contract_lock,
    }


def _verify_known_host_fingerprint(raw: bytes, *, host_id: str, expected_fingerprint: str) -> None:
    """Require an exact plain known_hosts entry whose key matches the pin."""

    host_tokens = {host_id}
    expected_token = expected_fingerprint.partition(":")[2]
    matching_entries = 0
    for raw_line in raw.splitlines():
        try:
            line = raw_line.decode("ascii").strip()
        except UnicodeDecodeError:
            continue
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3 or fields[0].startswith("|") or fields[0].startswith("@"):
            continue
        if not host_tokens.intersection(fields[0].split(",")):
            continue
        if len(fields) != 3:
            raise PlacementError("known_hosts peer entry is not a single pinned key")
        try:
            key = base64.b64decode(fields[2], validate=True)
        except (ValueError, binascii.Error):
            raise PlacementError("known_hosts peer key is malformed") from None
        actual = base64.b64encode(hashlib.sha256(key).digest()).decode("ascii").rstrip("=")
        if actual != expected_token:
            raise PlacementError("known_hosts peer key differs from the configured fingerprint")
        matching_entries += 1
    if matching_entries != 1:
        raise PlacementError("known_hosts must contain exactly one pinned peer host key")


def _valid_host_name(value: str) -> bool:
    """Accept only a literal IP address or a bounded DNS name for SSH HostName."""

    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        labels = value.split(".")
        return (
            len(value) <= 253
            and bool(labels)
            and all(_HOSTNAME_LABEL_RE.fullmatch(label) for label in labels)
        )


def _read_trusted_bytes(path: Path, *, expected_uid: int, max_bytes: int, private: bool) -> bytes:
    """Read one protected regular file with O_NOFOLLOW and an explicit size cap."""

    _verify_path_chain(path.parent, expected_uid=expected_uid)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PlacementError("trusted file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, expected_uid}
            or info.st_nlink != 1
            or (private and mode & 0o077)
            or (not private and mode & 0o022)
            or info.st_size < 1
            or info.st_size > max_bytes
        ):
            raise PlacementError("trusted file ownership, mode, or size is unsafe")
        raw = os.read(descriptor, max_bytes + 1)
        if len(raw) > max_bytes:
            raise PlacementError("trusted file exceeds its size limit")
        return raw
    finally:
        os.close(descriptor)


def filter_group_members(
    context: PlacementContext | None,
    group: dict[str, Any],
    local_supported_ids: set[str] | frozenset[str],
    active_ids: set[str] | frozenset[str],
    requested_ids: set[str] | frozenset[str] = frozenset(),
) -> PlacementSelection | None:
    """Select only role-local required, explicitly requested, or active members."""

    if context is None:
        return None
    role = context.local_role
    all_ids = {
        item.get("componentId")
        for item in group.get("members", [])
        if isinstance(item, dict) and isinstance(item.get("componentId"), str)
    }
    if not all_ids:
        raise PlacementError("signed compatibility group members are unavailable")
    active = set(active_ids) & all_ids
    requested = set(requested_ids) & all_ids
    foreign_active = active - role.allowed_members
    if foreign_active:
        raise PlacementError("active compatibility members belong to another host role")
    missing = role.required_members - set(local_supported_ids)
    if missing:
        raise PlacementError("required local host-role component has no supported target")
    unsupported_requested = requested - set(local_supported_ids)
    if unsupported_requested:
        raise PlacementError("requested role component has no supported local target")
    included = set(role.required_members) | active | requested
    if not included <= role.allowed_members:
        raise PlacementError("requested compatibility member is outside the local host role")
    if not included <= set(local_supported_ids):
        raise PlacementError("active role component has no supported local target")
    excluded = tuple(sorted(all_ids - role.allowed_members))
    return PlacementSelection(tuple(sorted(included)), excluded)


def _exact_keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PlacementError(f"peer {label} fields are invalid")
    return value


def _valid_component_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PlacementError(f"peer {label} must be an array")
    required_keys = {
        "componentId",
        "manifestDigest",
        "artifactDigest",
        "targetId",
        "releaseIdentity",
        "identityAttested",
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        component = _exact_keys(item, required_keys, label)
        component_id = component.get("componentId")
        if (
            not isinstance(component_id, str)
            or not _ID_RE.fullmatch(component_id)
            or component_id in seen
            or not isinstance(component.get("manifestDigest"), str)
            or not _DIGEST_RE.fullmatch(component["manifestDigest"])
            or not isinstance(component.get("artifactDigest"), str)
            or not _DIGEST_RE.fullmatch(component["artifactDigest"])
            or not isinstance(component.get("targetId"), str)
            or not _ID_RE.fullmatch(component["targetId"])
            or component.get("releaseIdentity") != component.get("manifestDigest")
            or component.get("identityAttested") is not True
        ):
            raise PlacementError(f"peer {label} contains an invalid or unattested component")
        seen.add(component_id)
        result.append(component)
    if [item["componentId"] for item in result] != sorted(seen):
        raise PlacementError(f"peer {label} components are not in canonical order")
    return result


def _valid_desired_components(value: Any) -> list[dict[str, Any]]:
    """Validate the exact peer target set used as the stable placement binding."""

    if not isinstance(value, list):
        raise PlacementError("peer desiredComponents must be an array")
    required_keys = {
        "componentId",
        "version",
        "manifestDigest",
        "artifactDigest",
        "targetId",
        "releaseId",
        "sourceCommit",
        "sourceRepository",
        "channel",
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        item = _exact_keys(raw, required_keys, "desired component")
        component_id = item.get("componentId")
        version = item.get("version")
        release_id = item.get("releaseId")
        repository = item.get("sourceRepository")
        channel = item.get("channel")
        if (
            not isinstance(component_id, str)
            or not _ID_RE.fullmatch(component_id)
            or component_id in seen
            or not isinstance(version, str)
            or not version
            or len(version) > 128
            or any(ord(char) < 0x21 for char in version)
            or not isinstance(item.get("manifestDigest"), str)
            or not _DIGEST_RE.fullmatch(item["manifestDigest"])
            or not isinstance(item.get("artifactDigest"), str)
            or not _DIGEST_RE.fullmatch(item["artifactDigest"])
            or not isinstance(item.get("targetId"), str)
            or not _ID_RE.fullmatch(item["targetId"])
            or not isinstance(release_id, str)
            or not release_id
            or len(release_id) > 256
            or any(ord(char) < 0x21 for char in release_id)
            or not isinstance(item.get("sourceCommit"), str)
            or not _COMMIT_RE.fullmatch(item["sourceCommit"])
            or not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not isinstance(channel, str)
            or not _ID_RE.fullmatch(channel)
        ):
            raise PlacementError("peer desired component identity is invalid")
        seen.add(component_id)
        result.append(item)
    if [item["componentId"] for item in result] != sorted(seen):
        raise PlacementError("peer desired components are not in canonical order")
    return result


def _validate_service_inventory(
    value: Any,
    *,
    required_ids: frozenset[str],
    required_units: dict[str, str],
    expected_executable_paths: dict[str, str] | None,
    expected_executable_digests: dict[str, str] | None,
    active: bool,
    allowed_ids: frozenset[str],
) -> set[str]:
    """Validate complete required-unit state against signed expectations."""

    if not isinstance(value, list):
        raise PlacementError("peer services inventory must be an array")
    seen: set[str] = set()
    for raw in value:
        service = _exact_keys(
            raw,
            {
                "componentId",
                "unit",
                "activeState",
                "subState",
                "mainPid",
                "executablePath",
                "executableDigest",
                "processExecutableDigest",
                "identityAttested",
                "health",
            },
            "service",
        )
        component_id = service.get("componentId")
        unit = service.get("unit")
        executable_path = service.get("executablePath")
        expected_digest = (
            expected_executable_digests.get(component_id)
            if isinstance(component_id, str) and expected_executable_digests is not None
            else None
        )
        if (
            not isinstance(component_id, str)
            or component_id not in allowed_ids
            or component_id in seen
            or not isinstance(unit, str)
            or not unit.endswith(".service")
            or not _ID_RE.fullmatch(unit[:-8])
            or (component_id in required_units and unit != required_units[component_id])
            or not isinstance(executable_path, str)
            or not executable_path.startswith("/")
            or ".." in Path(executable_path).parts
            or expected_executable_paths is None
            or expected_executable_paths.get(component_id) != executable_path
            or not isinstance(expected_digest, str)
            or not _DIGEST_RE.fullmatch(expected_digest)
            or service.get("executableDigest") != expected_digest
            or service.get("identityAttested") is not True
        ):
            raise PlacementError("peer service identity does not match signed expectations")
        pid = service.get("mainPid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise PlacementError("peer service PID is invalid")
        if active:
            if (
                service.get("activeState") != "active"
                or service.get("subState") != "running"
                or pid <= 1
                or service.get("processExecutableDigest") != expected_digest
                or service.get("health") != "healthy"
            ):
                raise PlacementError("peer ACTIVE service identity or health is invalid")
        elif (
            service.get("activeState") != "inactive"
            or service.get("subState") != "dead"
            or pid != 0
            or service.get("processExecutableDigest") is not None
            or service.get("health") is not None
        ):
            raise PlacementError("peer PREPARED_HELD service is not confirmed inactive")
        seen.add(component_id)
    if [item["componentId"] for item in value] != sorted(seen):
        raise PlacementError("peer services inventory is not in canonical order")
    if not set(required_ids) <= seen:
        raise PlacementError("peer services inventory omits a required unit")
    if not seen <= set(required_units):
        raise PlacementError("peer services inventory includes a unit without signed expectations")
    if expected_executable_paths is None or not seen <= set(expected_executable_paths):
        raise PlacementError("signed executable paths are required for service evidence")
    if expected_executable_digests is None or not seen <= set(expected_executable_digests):
        raise PlacementError("signed executable expectations are required for service evidence")
    return seen


def _role_admission_scope(role_id: str) -> str:
    if role_id == "control-host":
        return "workspace-product-v2/control-host"
    if role_id == "connector-host":
        return "kernel-task-and-runtime-admission"
    raise PlacementError("peer role has no admission scope")


def _validate_control_admission(
    raw: Any, hold: Any, context: PlacementContext
) -> tuple[str, dict[str, Any]]:
    value = _exact_keys(
        raw, {"status", "apiReady", "executionReady", "businessReady", "gate"}, "control admission"
    )
    gate = _exact_keys(
        value.get("gate"),
        {
            "source",
            "scope",
            "state",
            "generation",
            "catalogDigest",
            "topologyDigest",
            "planDigest",
            "adoptionHold",
            "readerGid",
        },
        "control gate",
    )
    if (
        value.get("status") not in {"ready", "not_ready"}
        or any(
            not isinstance(value.get(key), bool)
            for key in ("apiReady", "executionReady", "businessReady")
        )
        or gate.get("source") != "cy-workspace-control-plane.deployment-admission.v1"
        or gate.get("scope") != "workspace-product-v2/control-host"
        or gate.get("state") not in {"closed", "open"}
        or not isinstance(gate.get("generation"), int)
        or isinstance(gate.get("generation"), bool)
        or gate["generation"] < 1
        or gate.get("catalogDigest") != context.catalog_digest
        or gate.get("topologyDigest") != context.topology_digest
        or not isinstance(gate.get("planDigest"), str)
        or not _DIGEST_RE.fullmatch(gate["planDigest"])
        or not isinstance(gate.get("readerGid"), int)
        or isinstance(gate.get("readerGid"), bool)
        or not 0 <= gate["readerGid"] <= 0xFFFFFFFF
    ):
        raise PlacementError("control admission evidence is unknown or mismatched")
    adoption_hold = gate.get("adoptionHold")
    if adoption_hold is not None:
        adoption_hold = _exact_keys(
            adoption_hold,
            {
                "requestId",
                "organizationId",
                "workspaceId",
                "authorityInstanceId",
                "phase",
                "planDigest",
            },
            "control adoption hold",
        )
        if (
            not _valid_identity_field(adoption_hold.get("requestId"), max_length=128)
            or adoption_hold.get("organizationId") != context.workspace_identity["organizationId"]
            or adoption_hold.get("workspaceId") != context.workspace_identity["workspaceId"]
            or adoption_hold.get("authorityInstanceId")
            != context.workspace_identity["authorityInstanceId"]
            or adoption_hold.get("phase") not in {"PREPARED_HELD", "ACTIVE_HELD", "ACTIVE"}
            or adoption_hold.get("planDigest") != gate["planDigest"]
        ):
            raise PlacementError("control adoption hold identity is invalid")
    phase = adoption_hold.get("phase") if adoption_hold is not None else None
    if phase == "ACTIVE":
        if hold is not None:
            raise PlacementError("settled control admission must not retain a hold proof")
    elif hold != adoption_hold:
        raise PlacementError("control admission hold does not match the fixed gate state")
    state = gate["state"]
    if state == "open" and (phase != "ACTIVE" or hold is not None):
        raise PlacementError("control gate reports open without a released adoption hold")
    if state == "closed" and phase == "ACTIVE":
        raise PlacementError("control gate is closed after reporting released adoption")
    if value["status"] != ("ready" if value["apiReady"] else "not_ready"):
        raise PlacementError("control status contradicts apiReady")
    if phase == "PREPARED_HELD" and (
        value["apiReady"] or value["executionReady"] or value["businessReady"]
    ):
        raise PlacementError("control PREPARED_HELD requires inactive control services")
    if phase == "ACTIVE_HELD" and (
        state != "closed"
        or value["status"] != "ready"
        or not value["apiReady"]
        or value["executionReady"]
        or value["businessReady"]
    ):
        raise PlacementError("control ACTIVE_HELD readiness contradicts its closed gate")
    if phase == "ACTIVE" and (
        state != "open"
        or value["status"] != "ready"
        or not value["apiReady"]
        or not value["executionReady"]
        or not value["businessReady"]
    ):
        raise PlacementError("control settled ACTIVE is not genuinely ready and open")
    return state, value


def _validate_connector_admission(
    raw: Any, hold: Any, context: PlacementContext, desired: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise PlacementError("connector admission evidence is malformed")
    source = raw.get("source")
    scope = raw.get("scope")
    state = raw.get("state")
    if source == "runtime-maintenance.ValidateMaintenanceHold":
        value = _exact_keys(raw, {"source", "scope", "state", "proofs"}, "connector hold evidence")
        if scope != "kernel-task-and-runtime-admission" or state != "closed":
            raise PlacementError("connector hold scope or state is invalid")
        summary = _exact_keys(
            hold,
            {
                "kind",
                "requestId",
                "planId",
                "planDigest",
                "componentArtifactDigests",
                "gateGeneration",
                "catalogGeneration",
            },
            "connector admission hold",
        )
        component_digests = summary.get("componentArtifactDigests")
        desired_digests = {item["componentId"]: item["artifactDigest"] for item in desired}
        if (
            summary.get("kind") != "CORE_RUNTIME"
            or not _valid_identity_field(summary.get("requestId"), max_length=128)
            or not isinstance(summary.get("planId"), str)
            or not _PLAN_ID_RE.fullmatch(summary["planId"])
            or not isinstance(summary.get("planDigest"), str)
            or not _DIGEST_RE.fullmatch(summary["planDigest"])
            or component_digests != desired_digests
            or not _positive_u64(summary.get("gateGeneration"))
            or not _positive_u64(summary.get("catalogGeneration"))
        ):
            raise PlacementError("connector hold does not match its protected plan")
        proofs = value.get("proofs")
        if not isinstance(proofs, list) or not proofs:
            raise PlacementError("connector hold has no ValidateMaintenanceHold proofs")
        expected_ids = set(desired_digests)
        seen: set[str] = set()
        for raw_proof in proofs:
            proof = _exact_keys(
                raw_proof,
                {
                    "valid",
                    "request_id",
                    "target_kind",
                    "plan",
                    "component_id",
                    "artifact_digest",
                    "gate_generation",
                    "catalog_generation",
                },
                "maintenance hold proof",
            )
            proof_plan = _exact_keys(
                proof.get("plan"),
                {"plan_id", "plan_digest", "component_artifact_digests"},
                "maintenance proof plan",
            )
            component_id = proof.get("component_id")
            if (
                proof.get("valid") is not True
                or proof.get("request_id") != summary["requestId"]
                or proof.get("target_kind") != "CORE_RUNTIME"
                or proof_plan.get("plan_id") != summary["planId"]
                or proof_plan.get("plan_digest") != summary["planDigest"]
                or proof_plan.get("component_artifact_digests") != desired_digests
                or not isinstance(component_id, str)
                or component_id not in expected_ids
                or component_id in seen
                or proof.get("artifact_digest") != desired_digests[component_id]
                or not _positive_u64(proof.get("gate_generation"))
                or proof.get("gate_generation") != summary["gateGeneration"]
                or not _positive_u64(proof.get("catalog_generation"))
                or proof.get("catalog_generation") != summary["catalogGeneration"]
            ):
                raise PlacementError("ValidateMaintenanceHold result does not match the plan")
            seen.add(component_id)
        if seen != expected_ids:
            raise PlacementError("connector hold proof set is incomplete")
        return "closed", value
    if source == "runtime-maintenance.GetUpdateReadiness":
        value = _exact_keys(
            raw,
            {"source", "scope", "state", "gateGeneration", "catalogGeneration", "readiness"},
            "connector readiness evidence",
        )
        if (
            scope != "kernel-task-and-runtime-admission"
            or state not in {"open", "unknown"}
            or hold is not None
            or not _positive_u64(value.get("gateGeneration"))
            or not _positive_u64(value.get("catalogGeneration"))
        ):
            raise PlacementError("connector open readiness identity is invalid")
        readiness = _exact_keys(
            value.get("readiness"),
            {
                "status",
                "gate_generation",
                "install_catalog_generation",
                "active_task_count",
                "active_tasks",
                "inflight_runtime_admission_count",
                "active_binding_operation_count",
                "active_binding_operations",
                "unknown_activity_sources",
                "active_worker_count",
                "active_allocation_count",
                "blocker_codes",
                "requires_restart_confirmation",
            },
            "connector readiness snapshot",
        )
        if (
            readiness.get("status")
            not in {
                "READY",
                "ACTIVE_TASKS",
                "ACTIVE_BINDING_OPERATIONS",
                "UNKNOWN",
                "IDLE_RUNTIME_REQUIRES_UNLOAD",
                "MAINTENANCE_ACTIVE",
                "USER_CONFIRMATION_REQUIRED",
                "STALE_READINESS",
            }
            or readiness.get("gate_generation") != value["gateGeneration"]
            or readiness.get("install_catalog_generation") != value["catalogGeneration"]
            or any(
                not isinstance(readiness.get(key), int)
                or isinstance(readiness.get(key), bool)
                or readiness[key] < 0
                for key in (
                    "active_task_count",
                    "inflight_runtime_admission_count",
                    "active_binding_operation_count",
                    "active_worker_count",
                    "active_allocation_count",
                )
            )
            or not isinstance(readiness.get("active_tasks"), list)
            or readiness.get("active_task_count") != len(readiness["active_tasks"])
            or not isinstance(readiness.get("active_binding_operations"), list)
            or readiness.get("active_binding_operation_count")
            != len(readiness["active_binding_operations"])
            or not isinstance(readiness.get("unknown_activity_sources"), list)
            or not isinstance(readiness.get("blocker_codes"), list)
            or not isinstance(readiness.get("requires_restart_confirmation"), bool)
        ):
            raise PlacementError("connector readiness snapshot is malformed")
        unknown_status = readiness["status"] in {"UNKNOWN", "STALE_READINESS"}
        if (state == "unknown") != unknown_status:
            raise PlacementError("connector readiness state contradicts its status")
        if readiness["status"] == "READY" and (
            any(
                readiness[key] != 0
                for key in (
                    "active_task_count",
                    "inflight_runtime_admission_count",
                    "active_binding_operation_count",
                    "active_worker_count",
                    "active_allocation_count",
                )
            )
            or readiness["active_tasks"]
            or readiness["active_binding_operations"]
            or readiness["unknown_activity_sources"]
            or readiness["blocker_codes"]
            or readiness["requires_restart_confirmation"]
        ):
            raise PlacementError("connector READY snapshot contains active usage")
        return state, value
    raise PlacementError("connector business-admission source is not trusted")


def _positive_u64(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 0xFFFFFFFFFFFFFFFF
    )


def _hold_matches_phase(hold: Any, phase: str) -> bool:
    if not isinstance(hold, dict):
        return False
    return hold.get("phase") == phase or hold.get("kind") == "CORE_RUNTIME"


def _components_match_desired(
    observed: list[dict[str, Any]], desired: list[dict[str, Any]]
) -> bool:
    expected = {
        item["componentId"]: (
            item["manifestDigest"],
            item["artifactDigest"],
            item["targetId"],
        )
        for item in desired
    }
    actual = {
        item["componentId"]: (
            item["manifestDigest"],
            item["artifactDigest"],
            item["targetId"],
        )
        for item in observed
    }
    return actual == expected


def validate_peer_receipt(
    context: PlacementContext,
    receipt: Any,
    *,
    expected_phase: str | None = None,
    expected_nonce: str | None = None,
    plan: dict[str, Any] | None = None,
    required_service_ids: frozenset[str] = frozenset(),
    required_service_units: dict[str, str] | None = None,
    required_service_executable_paths: dict[str, str] | None = None,
    required_service_executable_digests: dict[str, str] | None = None,
    now: int | None = None,
) -> PeerEvidence:
    """Validate fresh, nonce-bound evidence returned by the pinned peer helper."""

    keys = {
        "schemaVersion",
        "deploymentId",
        "workspaceIdentity",
        "topologyDigest",
        "hostConfigDigest",
        "catalogDigest",
        "groupId",
        "groupVersion",
        "contractLock",
        "requesterHostId",
        "requesterRoleId",
        "hostId",
        "roleId",
        "phase",
        "challengeNonce",
        "issuedAt",
        "expiresAt",
        "planId",
        "planDigest",
        "desiredComponents",
        "activePointer",
        "stagedComponents",
        "activeComponents",
        "transaction",
        "admissionHold",
        "businessAdmission",
        "services",
    }
    value = _exact_keys(receipt, keys, "receipt")
    phase = value.get("phase")
    issued_at = value.get("issuedAt")
    expires_at = value.get("expiresAt")
    current = int(time.time()) if now is None else now
    if (
        value.get("schemaVersion") != 1
        or value.get("deploymentId") != context.deployment_id
        or value.get("workspaceIdentity") != context.workspace_identity
        or value.get("topologyDigest") != context.topology_digest
        or not isinstance(value.get("hostConfigDigest"), str)
        or not _DIGEST_RE.fullmatch(value["hostConfigDigest"])
        or value.get("catalogDigest") != context.catalog_digest
        or value.get("groupId") != context.group_id
        or value.get("groupVersion") != context.group_version
        or value.get("contractLock") != context.contract_lock
        or value.get("requesterHostId") != context.host_id
        or value.get("requesterRoleId") != context.role_id
        or value.get("hostId") != context.peer.host_id
        or value.get("roleId") != context.peer.role_id
        or not isinstance(phase, str)
        or phase not in _PHASES
        or (expected_phase is not None and phase != expected_phase)
        or not isinstance(value.get("challengeNonce"), str)
        or not _NONCE_RE.fullmatch(value["challengeNonce"])
        or (expected_nonce is not None and value["challengeNonce"] != expected_nonce)
        or not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_PEER_TTL_SECONDS
        or issued_at > current + MAX_CLOCK_SKEW_SECONDS
        or expires_at < current - MAX_CLOCK_SKEW_SECONDS
    ):
        raise PlacementError("peer receipt identity, phase, or freshness is invalid")
    staged = _valid_component_list(value.get("stagedComponents"), "stagedComponents")
    active = _valid_component_list(value.get("activeComponents"), "activeComponents")
    pointer = _exact_keys(
        value.get("activePointer"), {"present", "digest", "components"}, "activePointer"
    )
    pointer_components = _valid_component_list(
        pointer.get("components"), "activePointer.components"
    )
    transaction = _exact_keys(value.get("transaction"), {"present", "phase"}, "transaction")
    if (
        not isinstance(pointer.get("present"), bool)
        or not isinstance(transaction.get("present"), bool)
        or not isinstance(transaction.get("phase"), str)
    ):
        raise PlacementError("peer transaction state is unknown")
    desired = _valid_desired_components(value.get("desiredComponents"))
    desired_ids = {item["componentId"] for item in desired}
    staged_ids = {item["componentId"] for item in staged}
    active_ids = {item["componentId"] for item in active}
    allowed_ids = context.peer_role.allowed_members
    required_ids = context.peer_role.required_members
    if not required_service_ids <= required_ids:
        raise PlacementError("required service IDs are outside the signed peer role")
    if (
        not staged_ids <= allowed_ids
        or not active_ids <= allowed_ids
        or not desired_ids <= allowed_ids
    ):
        raise PlacementError("peer reports a component assigned to another host role")
    plan_id = value.get("planId")
    plan_digest = value.get("planDigest")
    has_plan = isinstance(plan_id, str) and isinstance(plan_digest, str)
    if has_plan:
        if not _PLAN_ID_RE.fullmatch(plan_id) or not _DIGEST_RE.fullmatch(plan_digest):
            raise PlacementError("peer persisted plan identity is invalid")
        if not required_ids <= desired_ids:
            raise PlacementError("peer plan omits a required role component")
    elif plan_id is not None or plan_digest is not None:
        raise PlacementError("peer desired targets have no persisted plan identity")
    if plan is not None:
        expected_desired = _valid_desired_components(plan.get("desiredComponents"))
        if (
            plan.get("planId") != plan_id
            or plan.get("planDigest") != plan_digest
            or expected_desired != desired
        ):
            raise PlacementError("peer persisted plan changed")
    if desired and not required_ids <= desired_ids:
        raise PlacementError("peer desired target set is incomplete")

    # Parse role-specific admission evidence without reducing it to a boolean.
    admission = value.get("businessAdmission")
    hold = value.get("admissionHold")
    unavailable = {
        "source": "unavailable",
        "scope": _role_admission_scope(context.peer.role_id),
        "state": "unavailable",
    }
    if admission == unavailable:
        if phase not in {"ABSENT", "STAGED"} or hold is not None:
            raise PlacementError(
                "unavailable admission evidence cannot authorize held or active state"
            )
        admission_state = "unavailable"
    elif context.peer.role_id == "control-host":
        admission_state, _admission_details = _validate_control_admission(admission, hold, context)
    else:
        admission_state, _admission_details = _validate_connector_admission(
            admission, hold, context, desired
        )
        if phase in {"PREPARED_HELD", "ACTIVE_HELD"} and (
            not isinstance(hold, dict)
            or hold.get("planId") != plan_id
            or hold.get("planDigest") != plan_digest
        ):
            raise PlacementError("connector hold does not match the peer's persisted plan")

    if phase == "ABSENT":
        if (
            staged
            or active
            or has_plan
            or desired
            or pointer["present"]
            or pointer.get("digest") is not None
            or pointer_components
            or hold is not None
            or value.get("services") != []
        ):
            raise PlacementError("peer ABSENT receipt describes existing state")
    elif phase == "STAGED":
        observed_ids = staged_ids | active_ids
        if (
            not desired
            or not has_plan
            or not required_ids <= desired_ids
            or staged_ids & active_ids
            or observed_ids != desired_ids
            or not _components_match_desired([*staged, *active], desired)
            or (
                bool(active)
                and (
                    not pointer["present"]
                    or not isinstance(pointer.get("digest"), str)
                    or not _DIGEST_RE.fullmatch(pointer["digest"])
                    or pointer_components != active
                )
            )
            or (
                not active
                and (pointer["present"] or pointer.get("digest") is not None or pointer_components)
            )
            or hold is not None
        ):
            raise PlacementError("peer STAGED receipt does not match its staged and active targets")
    elif phase == "PREPARED_HELD":
        if (
            not desired
            or not has_plan
            or staged_ids != desired_ids
            or not _components_match_desired(staged, desired)
            or active
            or pointer["present"]
            or pointer.get("digest") is not None
            or pointer_components
            or transaction["present"]
            or transaction["phase"] != "none"
            or admission_state != "closed"
            or not _hold_matches_phase(hold, "PREPARED_HELD")
        ):
            raise PlacementError("peer PREPARED_HELD receipt lacks a bound hold or exact stage")
        _validate_service_inventory(
            value.get("services"),
            required_ids=required_service_ids,
            required_units=required_service_units or {},
            expected_executable_paths=required_service_executable_paths,
            expected_executable_digests=required_service_executable_digests,
            active=False,
            allowed_ids=allowed_ids,
        )
    elif phase in {"ACTIVE_HELD", "ACTIVE"}:
        if (
            not desired
            or (phase == "ACTIVE_HELD" and not has_plan)
            or active_ids != desired_ids
            or not _components_match_desired(active, desired)
            or not pointer["present"]
            or not isinstance(pointer.get("digest"), str)
            or not _DIGEST_RE.fullmatch(pointer["digest"])
            or pointer_components != active
            or transaction["present"]
            or transaction["phase"] != "none"
        ):
            raise PlacementError(
                "peer active receipt lacks exact active identities or transaction closure"
            )
        _validate_service_inventory(
            value.get("services"),
            required_ids=required_service_ids,
            required_units=required_service_units or {},
            expected_executable_paths=required_service_executable_paths,
            expected_executable_digests=required_service_executable_digests,
            active=True,
            allowed_ids=allowed_ids,
        )
        if phase == "ACTIVE_HELD":
            if admission_state != "closed" or not _hold_matches_phase(hold, "ACTIVE_HELD"):
                raise PlacementError("peer ACTIVE_HELD receipt lacks its current hold proof")
        elif (
            hold is not None
            or admission_state != "open"
            and not (context.peer.role_id == "connector-host" and admission_state == "unknown")
        ):
            raise PlacementError("peer settled ACTIVE receipt lacks real released admission proof")
    if phase in {"ABSENT", "STAGED"} and hold is not None:
        raise PlacementError("preparation-only peer state cannot carry an adoption hold")
    if phase in {"PREPARED_HELD", "ACTIVE_HELD"} and transaction["present"]:
        raise PlacementError(
            "held adoption state has an unrelated or unfinished update transaction"
        )

    stable = _peer_stable_projection(context, value, desired)
    stable_digest = "sha256:" + hashlib.sha256(_canonical_bytes(stable)).hexdigest()
    response_digest = "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()
    return PeerEvidence(value, stable_digest, response_digest)


def authorize_peer_operation(
    context: PlacementContext, evidence: PeerEvidence, operation: str
) -> None:
    """Enforce the two-phase cross-host order without treating stage as ready."""

    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise PlacementError("unsupported placement operation")
    if context.role_id not in {"connector-host", "control-host"}:
        raise PlacementError("host role has no supported two-phase operation order")
    pre_apply = {"ABSENT", "STAGED", "PREPARED_HELD", "ACTIVE_HELD", "ACTIVE"}
    apply = {"PREPARED_HELD", "ACTIVE_HELD", "ACTIVE"}
    permitted = {"check": pre_apply, "stage": pre_apply, "apply": apply}
    if evidence.phase not in permitted[operation]:
        raise PlacementError("peer phase does not permit this host operation")
    if operation == "apply" and context.peer.role_id == "connector-host":
        admission = evidence.payload.get("businessAdmission", {})
        readiness = admission.get("readiness")
        if evidence.phase == "ACTIVE" and (
            admission.get("state") != "open"
            or not isinstance(readiness, dict)
            or readiness.get("status") != "READY"
        ):
            raise PlacementError("connector peer must be idle and READY before apply")


def _collect_bounded(process: subprocess.Popen[bytes], *, timeout: float) -> tuple[bytes, bytes]:
    """Drain SSH pipes with hard byte and elapsed-time bounds."""

    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    collected: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": MAX_PEER_RESPONSE_BYTES, "stderr": MAX_PEER_STDERR_BYTES}
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlacementError("peer helper timed out")
            for key, _ in selector.select(min(remaining, 0.25)):
                stream = key.fileobj
                name = key.data
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    selector.unregister(stream)
                    continue
                collected[name].extend(chunk)
                if len(collected[name]) > limits[name]:
                    raise PlacementError("peer helper response exceeded its size limit")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise PlacementError("peer helper returned a failure status")
    except Exception:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
    if collected["stderr"]:
        raise PlacementError("peer helper wrote unexpected diagnostic output")
    return bytes(collected["stdout"]), bytes(collected["stderr"])


def _peer_ssh_json(
    context: PlacementContext,
    *,
    operation: str,
    timeout: float,
    expected_phase: str | None = None,
) -> tuple[dict[str, Any], str]:
    if operation not in {"placement-peer-plan", "placement-peer-receipt"}:
        raise PlacementError("unsupported peer helper operation")
    if timeout <= 0 or timeout > 60:
        raise PlacementError("peer SSH timeout is outside its permitted bound")
    if expected_phase is not None and expected_phase not in _PHASES:
        raise PlacementError("unsupported expected peer phase")
    nonce = secrets.token_hex(32)
    peer = context.peer
    argv = [
        SSH_EXECUTABLE,
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "ForwardAgent=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={peer.known_hosts_file}",
        "-o",
        f"HostName={peer.host_name}",
        "-o",
        f"HostKeyAlias={peer.host_id}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "CanonicalizeHostname=no",
        "-o",
        "CheckHostIP=no",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "RequestTTY=no",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=2",
        "-o",
        "ServerAliveCountMax=2",
        "-i",
        str(peer.identity_file),
        "-p",
        str(peer.ssh_port),
        "-l",
        peer.ssh_user,
        peer.host_id,
        SUDO_EXECUTABLE,
        "-n",
        PEER_HELPER,
        operation,
        "--deployment-id",
        context.deployment_id,
        "--requester-host-id",
        context.host_id,
        "--requester-role-id",
        context.role_id,
        "--group-id",
        context.group_id,
        "--nonce",
        nonce,
    ]
    if operation == "placement-peer-receipt":
        argv.extend(["--phase", expected_phase or "any"])
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except OSError as exc:
        raise PlacementError("pinned peer helper could not be started") from exc
    stdout, _stderr = _collect_bounded(process, timeout=timeout)
    try:
        value = json.loads(stdout, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlacementError("peer helper returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise PlacementError("peer helper response must be a JSON object")
    return value, nonce


def _peer_stable_projection(
    context: PlacementContext, value: dict[str, Any], desired: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "deploymentId": context.deployment_id,
        "workspaceIdentity": context.workspace_identity,
        "topologyDigest": context.topology_digest,
        "hostConfigDigest": value["hostConfigDigest"],
        "catalogDigest": context.catalog_digest,
        "groupId": context.group_id,
        "groupVersion": context.group_version,
        "contractLock": context.contract_lock,
        "requesterHostId": context.host_id,
        "requesterRoleId": context.role_id,
        "hostId": context.peer.host_id,
        "roleId": context.peer.role_id,
        "desiredComponents": desired,
    }


def _validate_plan_response(
    context: PlacementContext,
    value: Any,
    *,
    nonce: str,
    now: int,
) -> PeerPlanEvidence:
    keys = {
        "schemaVersion",
        "deploymentId",
        "workspaceIdentity",
        "topologyDigest",
        "hostConfigDigest",
        "catalogDigest",
        "groupId",
        "groupVersion",
        "contractLock",
        "requesterHostId",
        "requesterRoleId",
        "hostId",
        "roleId",
        "challengeNonce",
        "issuedAt",
        "expiresAt",
        "planId",
        "planDigest",
        "desiredComponents",
    }
    payload = _exact_keys(value, keys, "persisted plan")
    issued = payload.get("issuedAt")
    expires = payload.get("expiresAt")
    plan_id = payload.get("planId")
    plan_digest = payload.get("planDigest")
    if (
        payload.get("schemaVersion") != 1
        or payload.get("deploymentId") != context.deployment_id
        or payload.get("workspaceIdentity") != context.workspace_identity
        or payload.get("topologyDigest") != context.topology_digest
        or not isinstance(payload.get("hostConfigDigest"), str)
        or not _DIGEST_RE.fullmatch(payload["hostConfigDigest"])
        or payload.get("catalogDigest") != context.catalog_digest
        or payload.get("groupId") != context.group_id
        or payload.get("groupVersion") != context.group_version
        or payload.get("contractLock") != context.contract_lock
        or payload.get("requesterHostId") != context.host_id
        or payload.get("requesterRoleId") != context.role_id
        or payload.get("hostId") != context.peer.host_id
        or payload.get("roleId") != context.peer.role_id
        or payload.get("challengeNonce") != nonce
        or not isinstance(issued, int)
        or isinstance(issued, bool)
        or not isinstance(expires, int)
        or isinstance(expires, bool)
        or expires <= issued
        or expires - issued > MAX_PEER_TTL_SECONDS
        or issued > now + MAX_CLOCK_SKEW_SECONDS
        or expires < now - MAX_CLOCK_SKEW_SECONDS
        or (
            plan_id is not None
            and (not isinstance(plan_id, str) or not _PLAN_ID_RE.fullmatch(plan_id))
        )
        or (
            plan_digest is not None
            and (not isinstance(plan_digest, str) or not _DIGEST_RE.fullmatch(plan_digest))
        )
        or ((plan_id is None) != (plan_digest is None))
    ):
        raise PlacementError("peer persisted plan identity or freshness is invalid")
    desired = _valid_desired_components(payload.get("desiredComponents"))
    if plan_id is not None and not context.peer_role.required_members <= {
        item["componentId"] for item in desired
    }:
        raise PlacementError("peer persisted plan omits required role members")
    stable = _peer_stable_projection(context, payload, desired)
    stable_digest = "sha256:" + hashlib.sha256(_canonical_bytes(stable)).hexdigest()
    response_digest = "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return PeerPlanEvidence(payload, stable_digest, response_digest)


def fetch_peer_plan(
    context: PlacementContext, *, timeout: float = DEFAULT_SSH_TIMEOUT_SECONDS
) -> PeerPlanEvidence:
    """Fetch a read-only verified target plan; it conveys no readiness authority."""

    value, nonce = _peer_ssh_json(context, operation="placement-peer-plan", timeout=timeout)
    return _validate_plan_response(context, value, nonce=nonce, now=int(time.time()))


def fetch_peer_receipt(
    context: PlacementContext,
    *,
    expected_phase: str | None = None,
    plan: dict[str, Any] | None = None,
    required_service_ids: frozenset[str] = frozenset(),
    required_service_units: dict[str, str] | None = None,
    required_service_executable_paths: dict[str, str] | None = None,
    required_service_executable_digests: dict[str, str] | None = None,
    timeout: float = DEFAULT_SSH_TIMEOUT_SECONDS,
) -> PeerEvidence:
    """Fetch and validate one nonce-bound receipt via fixed, pinned SSH helper."""

    value, nonce = _peer_ssh_json(
        context,
        operation="placement-peer-receipt",
        timeout=timeout,
        expected_phase=expected_phase,
    )
    evidence = validate_peer_receipt(
        context,
        value,
        expected_phase=expected_phase,
        expected_nonce=nonce,
        plan=plan,
        required_service_ids=required_service_ids,
        required_service_units=required_service_units,
        required_service_executable_paths=required_service_executable_paths,
        required_service_executable_digests=required_service_executable_digests,
        now=int(time.time()),
    )
    return evidence
