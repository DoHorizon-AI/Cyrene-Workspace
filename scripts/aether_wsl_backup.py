#!/usr/bin/env python3
"""
┌────────────────────────────────────────────────────────────────────────────┐
│ Module: scripts.aether_wsl_backup                                          │
│ Role: Snapshot and restore complete local Aether application state safely. │
│                                                                            │
│ 模块职责：通过显式资源清单备份、验证并恢复本地 Aether 应用状态。             │
└────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gzip
import hashlib
import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

STATE_DIR = Path("/home/baijin/.local/state/cyrene-aether-wsl-acceptance")
PROJECT_NAME = "cyrene-aether-wsl-acceptance"
COMPOSE_FILE = Path(__file__).resolve().parents[1] / "tooling/aether-wsl-acceptance/compose.yaml"
DEFAULT_OUTPUT_DIR = STATE_DIR.parent / "cyrene-aether-wsl-backups"
CATALOG_FORMAT = "cyrene.aether.backup-catalog.v1"
QUIESCENCE_FORMAT = "cyrene.aether.quiescence.v1"
SNAPSHOT_FORMAT = "cyrene.aether.application-state-snapshot.v1"
RESTORE_FORMAT = "cyrene.aether.restore-receipt.v1"
ROLLBACK_FORMAT = "cyrene.aether.rollback-receipt.v1"
MANIFEST_NAME = "manifest.json"
RECEIPT_NAME = "receipt.json"
CHUNK_SIZE = 1024 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_RESOURCES = 256
MAX_RESOURCE_FILES = 20_000
MAX_TOTAL_PAYLOAD_FILES = 50_000
MAX_DECLARED_EXCLUSIONS = 256
MAX_DATABASE_TABLES = 20_000

REQUIRED_CATEGORIES = {
    "navigator_business",
    "navigator_dsh",
    "navigator_attachments",
    "exchange_database",
    "exchange_audit",
    "plugin_database",
    "plugin_blobs",
    "postgres_database",
}
REQUIRED_RESOURCE_PATHS = {
    "navigator/navigator.sqlite3": ("sqlite", "navigator_business"),
    "navigator/navigator.sqlite3.conversation-memory.sqlite3": (
        "sqlite",
        "navigator_business",
    ),
    "navigator/navigator.sqlite3.conversation-domains.sqlite3": (
        "sqlite",
        "navigator_business",
    ),
    "navigator/navigator.sqlite3.work-attachments": ("tree", "navigator_attachments"),
    "navigator/artifacts": ("tree", "navigator_attachments"),
    "navigator/dsh-home": ("tree", "navigator_dsh"),
    "navigator/providers/memory.sqlite3": ("sqlite", "plugin_database"),
    "navigator/providers/knowledge-graph.sqlite3": ("sqlite", "plugin_database"),
    "local-product/exchange-product.sqlite3": ("sqlite", "exchange_database"),
}
EXCHANGE_DATABASE_PATH = "local-product/exchange-product.sqlite3"
EXCHANGE_SECRET_POLICY = "external_refs_plus_approved_exchange_verifier"
EXCHANGE_API_CREDENTIAL_SCHEMA = (
    ("credential_ref", "TEXT", 0, None, 1),
    ("token_digest", "TEXT", 1, None, 0),
    ("actor_id", "TEXT", 1, None, 0),
    ("workspace_id", "TEXT", 1, None, 0),
    ("organization_id", "TEXT", 0, None, 0),
    ("enabled", "INTEGER", 1, "1", 0),
    ("created_at", "INTEGER", 1, None, 0),
    ("api_key_id", "TEXT", 0, None, 0),
    ("name", "TEXT", 0, None, 0),
    ("expires_at", "INTEGER", 0, None, 0),
    ("revoked_at", "INTEGER", 0, None, 0),
    ("model_scope", "TEXT", 0, None, 0),
    ("resource_version", "INTEGER", 1, "1", 0),
)
QUIESCENT_COMPONENTS = {"navigator", "dsh", "exchange", "plugins", "postgres"}
CATEGORY_KINDS = {
    "navigator_business": {"sqlite"},
    "navigator_dsh": {"sqlite", "tree"},
    "navigator_attachments": {"tree"},
    "exchange_database": {"sqlite"},
    "exchange_audit": {"sqlite"},
    "plugin_database": {"sqlite"},
    "plugin_blobs": {"tree"},
    "postgres_database": {"postgres"},
    "model_reference": {"reference"},
    "toolchain_reference": {"reference"},
    "cache_reference": {"reference"},
}
EXCLUSION_CATEGORIES = {
    "plaintext_credentials_session_tokens_secret_files",
    "postgres_physical_database_files",
    "logs_evidence_test_fixtures",
}
REQUIRED_EXCLUSION_CATEGORIES = set(EXCLUSION_CATEGORIES)
MANIFEST_EXTRA_EXCLUSION_CATEGORIES = {"postgres_restore_drill_databases"}
OWNER_NAMES = {"navigator", "exchange", "plugins", "workspace"}
SENSITIVE_NAME = re.compile(
    r"(^|[^a-z])(secret|password|passwd|access[_-]?token|refresh[_-]?token|"
    r"bearer[_-]?token|api[_-]?key|private[_-]?key|"
    r"credential[_-]?(?:value|blob|data|bytes|token|secret|payload|ciphertext))([^a-z]|$)",
    re.IGNORECASE,
)
SENSITIVE_TABLE_NAME = re.compile(
    r"(^|[^a-z])(secret|secrets|credential|credentials|token|tokens|passwords?|passwds?|api[_-]?keys?)([^a-z]|$)",
    re.IGNORECASE,
)
SECRET_REFERENCE_SUFFIX = re.compile(r"(?:_ref|_reference|_id|_uri|_url)$", re.IGNORECASE)
SAFE_CREDENTIAL_METADATA_COLUMNS = {
    "id",
    "owner_id",
    "workspace_id",
    "organization_id",
    "credential_ref",
    "secret_ref",
    "token_ref",
    "password_ref",
    "api_key_ref",
    "provider",
    "name",
    "scope",
    "status",
    "created_at",
    "updated_at",
    "expires_at",
}
SECRET_PATH_PART = re.compile(
    r"(^|[._-])(secret|secrets|credential|credentials|token|tokens|auth)([._-]|$)",
    re.IGNORECASE,
)
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".env"}
REFERENCE_PATH_PART = re.compile(
    r"(^|/)(models?|toolchain|cache|uv-cache|node_modules|\.venv)(/|$)",
    re.IGNORECASE,
)
SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
ENV_REF = re.compile(
    r"^(?:env:[A-Z][A-Z0-9_]{2,127}|"
    r"cred:(?://)?[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*){1,8})$"
)
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DB_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_$]{0,62}$")


class BackupError(RuntimeError):
    """Raised when a backup or restore precondition cannot be proven safely.

    中文：无法安全证明备份或恢复前置条件时抛出。
    """


def _now() -> dt.datetime:
    """Returns an aware UTC timestamp for receipts and journals.

    中文：生成回执和日志使用的 UTC 时间。
    """
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _sha256_file(path: Path) -> tuple[str, int]:
    """Hashes one owned regular file using bounded reads.

    中文：以有界内存读取并计算普通文件的 SHA-256。
    """
    info = _lstat_owned(path, regular=True)
    digest = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while chunk := stream.read(CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
    finally:
        os.close(fd)
    if size != info.st_size:
        raise BackupError("source changed while hashing; retry after quiescence")
    return digest.hexdigest(), size


def _lstat_owned(
    path: Path, *, regular: bool | None = None, directory: bool | None = None
) -> os.stat_result:
    """Checks a path without following the final component.

    中文：不跟随路径末级链接，并验证当前用户所有权与文件类型。
    """
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError("required source path is missing") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BackupError("symlink paths are not supported")
    if info.st_uid != os.geteuid():
        raise BackupError("source path is not owned by the current user")
    if regular and not stat.S_ISREG(info.st_mode):
        raise BackupError("expected a regular file")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise BackupError("expected a directory")
    return info


def _safe_relative(value: str) -> PurePosixPath:
    """Validates a catalog path as a non-empty POSIX relative path.

    中文：验证清单路径是非空 POSIX 相对路径，拒绝遍历和映射。
    """
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BackupError("catalog contains an invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise BackupError("catalog path traversal or remapping is not allowed")
    return path


def _walk_no_follow(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Walks an owned source tree without descending through links.

    中文：遍历归属当前用户的源目录，不跟随任何符号链接。
    """
    stack = [root]
    while stack:
        directory = stack.pop()
        _lstat_owned(directory, directory=True)
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise BackupError("cannot read source tree") from exc
        for entry in entries:
            info = _lstat_owned(entry)
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry)
            elif stat.S_ISREG(info.st_mode):
                yield entry, info
            else:
                raise BackupError("special files are not supported in application state")


def _is_secret_path(relative: PurePosixPath) -> bool:
    """Identifies credential files by path only; their contents are never read.

    中文：只按文件名识别凭据文件，绝不读取凭据文件内容。
    """
    for part in relative.parts:
        lower = part.lower()
        if lower in {".env", "secrets", "credentials", "private"}:
            return True
        if SECRET_PATH_PART.search(part) or Path(part).suffix.lower() in SECRET_SUFFIXES:
            return True
    return False


def _ensure_no_symlink_components(
    path: Path, *, allow_missing_leaf: bool = False, allow_foreign_leaf: bool = False
) -> None:
    """Checks all existing absolute path components for links and foreign owners.

    中文：逐级检查绝对路径中已存在的部分，拒绝符号链接和非本人所有的组件。
    """
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:]):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(absolute.parts[1:]) - 1:
                return
            raise BackupError("a parent directory is missing") from None
        if stat.S_ISLNK(info.st_mode):
            raise BackupError("symlink path components are not supported")
        under_home = current == Path.home() or current.is_relative_to(Path.home())
        is_leaf = current == absolute
        if under_home and info.st_uid != os.geteuid() and not (allow_foreign_leaf and is_leaf):
            raise BackupError("path component is not owned by the current user")


def _secure_private_directory(path: Path, *, create: bool) -> None:
    """Creates or validates a private directory owned by the current user.

    中文：创建或验证当前用户拥有的私有目录。
    """
    _ensure_no_symlink_components(path, allow_missing_leaf=create)
    if not path.exists():
        if not create:
            raise BackupError("required directory does not exist")
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    info = _lstat_owned(path, directory=True)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupError("directory permissions must not grant group or other access")


def _secure_input_file(path: Path, *, max_bytes: int) -> bytes:
    """Reads a small private JSON input without following a symlink.

    中文：不跟随符号链接地读取小型私有 JSON 输入。
    """
    _ensure_no_symlink_components(path)
    _lstat_owned(path, regular=True)
    info = path.stat(follow_symlinks=False)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupError("catalog and receipt files must be private")
    if info.st_size > max_bytes:
        raise BackupError("catalog or receipt exceeds the supported size")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) != info.st_size or len(data) > max_bytes:
        raise BackupError("catalog or receipt changed while being read")
    return data


def _read_json(path: Path, *, max_bytes: int = MAX_CATALOG_BYTES) -> dict[str, Any]:
    try:
        value = json.loads(_secure_input_file(path, max_bytes=max_bytes))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("catalog or receipt is invalid JSON") from exc
    if not isinstance(value, dict):
        raise BackupError("catalog or receipt must be a JSON object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically publishes private JSON metadata and synchronizes its directory.

    中文：原子发布私有 JSON 元数据，并同步目录项。
    """
    _ensure_no_symlink_components(path.parent)
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        _lstat_owned(path, regular=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_stream(source: BinaryIO, destination: BinaryIO) -> tuple[str, int]:
    """Copies and hashes bytes with a fixed-size buffer.

    中文：使用固定大小缓冲区复制并计算内容摘要。
    """
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(CHUNK_SIZE):
        destination.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _copy_regular(source: Path, destination: Path) -> dict[str, Any]:
    """Copies one regular file into private staging without following links.

    中文：不跟随链接地将普通文件复制到私有暂存目录。
    """
    source_info = _lstat_owned(source, regular=True)
    source_identity = (
        source_info.st_dev,
        source_info.st_ino,
        source_info.st_uid,
        source_info.st_mode,
        source_info.st_size,
        source_info.st_mtime_ns,
        source_info.st_ctime_ns,
    )
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened_info = os.fstat(source_fd)
            if (
                not stat.S_ISREG(opened_info.st_mode)
                or opened_info.st_uid != os.geteuid()
                or _file_identity(opened_info) != source_identity
            ):
                raise BackupError("source changed while opening; retry after quiescence")
            with (
                os.fdopen(source_fd, "rb", closefd=False) as input_stream,
                os.fdopen(fd, "wb", closefd=False) as output_stream,
            ):
                digest, size = _copy_stream(input_stream, output_stream)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            copied_info = os.fstat(source_fd)
        finally:
            os.close(source_fd)
    finally:
        os.close(fd)
    final_info = _lstat_owned(source, regular=True)
    if (
        size != source_info.st_size
        or _file_identity(copied_info) != source_identity
        or _file_identity(final_info) != source_identity
    ):
        raise BackupError("source changed while copying; retry after quiescence")
    return {"sha256": digest, "size_bytes": size}


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    """Returns stable file metadata used to detect mutation during a copy.

    中文：返回复制期间用于检测文件变化的稳定元数据。
    """
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _catalog_digest(catalog: dict[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(catalog)).hexdigest()


def _validate_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """Validates a complete source inventory against the Aether backup contract.

    中文：按 Aether 备份契约验证完整的源资源清单。
    """
    if catalog.get("format") != CATALOG_FORMAT:
        raise BackupError("unsupported source catalog format")
    _secure_private_directory(STATE_DIR, create=False)
    if catalog.get("state_root") != str(STATE_DIR):
        raise BackupError("catalog state root does not match the fixed Aether task root")
    if catalog.get("compose_project") != PROJECT_NAME:
        raise BackupError("catalog Compose project does not match the fixed task project")
    revisions = catalog.get("source_revisions")
    required_revisions = {"navigator", "exchange", "plugins", "workspace"}
    if not isinstance(revisions, dict) or not required_revisions.issubset(revisions):
        raise BackupError("source catalog is missing canonical repository revisions")
    for owner in OWNER_NAMES:
        if not isinstance(revisions.get(owner), str) or not COMMIT.fullmatch(revisions[owner]):
            raise BackupError("source catalog contains an invalid source revision")
    resources = catalog.get("sources")
    if not isinstance(resources, list) or not 1 <= len(resources) <= MAX_RESOURCES:
        raise BackupError("source catalog must enumerate a bounded set of resources")
    covered: set[str] = set()
    identifiers: set[str] = set()
    source_paths: dict[str, str] = {}
    sources_by_path: dict[str, dict[str, Any]] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            raise BackupError("source catalog resource is invalid")
        resource_id = resource.get("id")
        kind = resource.get("kind")
        owner = resource.get("owner")
        categories = resource.get("categories")
        if not isinstance(resource_id, str) or not SLUG.fullmatch(resource_id):
            raise BackupError("source catalog resource ID is invalid")
        if resource_id in identifiers:
            raise BackupError("source catalog resource IDs must be unique")
        identifiers.add(resource_id)
        if kind not in {"sqlite", "tree", "postgres", "reference"}:
            raise BackupError("source catalog resource kind is unsupported")
        if owner not in OWNER_NAMES:
            raise BackupError("source catalog resource owner is unsupported")
        secret_policy = resource.get("secret_policy")
        approved_exchange_store = _is_approved_exchange_store(resource)
        expected_secret_policy = (
            EXCHANGE_SECRET_POLICY if approved_exchange_store else "external_refs_only"
        )
        if secret_policy != expected_secret_policy:
            raise BackupError("source credential policy is unsupported for this canonical resource")
        if not isinstance(categories, list) or not categories:
            raise BackupError("source catalog resource categories are required")
        if len(set(categories)) != len(categories):
            raise BackupError("source catalog resource categories must be unique")
        for category in categories:
            if category not in CATEGORY_KINDS or kind not in CATEGORY_KINDS[category]:
                raise BackupError("source catalog resource category and kind do not match")
            if category in REQUIRED_CATEGORIES:
                covered.add(category)
        if kind == "postgres":
            if owner != "workspace" or resource.get("service") != "postgres":
                raise BackupError("PostgreSQL source must be the task-owned Compose service")
            if resource.get("database") != "cyrene_aether":
                raise BackupError("PostgreSQL source database is outside the fixed task scope")
            if "path" in resource:
                raise BackupError("PostgreSQL source must use pg_dump, not a filesystem path")
        else:
            relative = _safe_relative(resource.get("path"))
            if _is_secret_path(relative):
                raise BackupError(
                    "secret files and credential directories cannot be declared as sources"
                )
            if kind == "tree" and _tree_exclusion_category(relative):
                raise BackupError(
                    "logs, fixture, cache, and reference roots cannot be declared as application trees"
                )
            path_text = relative.as_posix()
            if path_text in source_paths and resource_id != source_paths[path_text]:
                raise BackupError("source path is assigned to more than one resource")
            source_paths[path_text] = resource_id
            sources_by_path[path_text] = resource
            if kind == "reference" and not any(
                category.endswith("_reference") for category in categories
            ):
                raise BackupError("reference resources must use a reference category")
        source_revision = revisions.get(owner)
        if not source_revision:
            raise BackupError("source catalog resource has no source-version binding")
        if resource.get("source_revision", source_revision) != source_revision:
            raise BackupError(
                "resource source revision differs from its canonical repository revision"
            )
        resource["source_revision"] = source_revision
        if kind != "postgres":
            rel = _safe_relative(resource.get("path"))
            source = STATE_DIR.joinpath(*rel.parts)
            _ensure_no_symlink_components(source)
            if kind == "sqlite":
                _lstat_owned(source, regular=True)
            elif kind == "tree":
                _lstat_owned(source, directory=True)
            else:
                _lstat_owned(source)
    for path_text, (required_kind, required_category) in REQUIRED_RESOURCE_PATHS.items():
        source = sources_by_path.get(path_text)
        if (
            source is None
            or source.get("kind") != required_kind
            or required_category not in source.get("categories", [])
        ):
            raise BackupError("source catalog is missing a canonical Aether application store")
    missing = REQUIRED_CATEGORIES - covered
    if missing:
        raise BackupError(
            "source catalog is incomplete; required application-state categories are missing"
        )
    secret_refs = catalog.get("external_secret_refs")
    if not isinstance(secret_refs, list) or not secret_refs:
        raise BackupError("source catalog must declare required external secret references")
    for secret_ref in secret_refs:
        if not isinstance(secret_ref, str) or not ENV_REF.fullmatch(secret_ref):
            raise BackupError("external secret references must be opaque IDs, never secret values")
    exclusions = catalog.get("declared_exclusions")
    if (
        not isinstance(exclusions, list)
        or not exclusions
        or len(exclusions) > MAX_DECLARED_EXCLUSIONS
    ):
        raise BackupError("source catalog must declare non-application exclusions")
    excluded_ids: set[str] = set()
    excluded_categories: set[str] = set()
    for exclusion in exclusions:
        if not isinstance(exclusion, dict):
            raise BackupError("declared exclusion record is invalid")
        exclusion_id = exclusion.get("id")
        category = exclusion.get("category")
        treatment = exclusion.get("treatment")
        relative = _safe_relative(exclusion.get("path"))
        if (
            not isinstance(exclusion_id, str)
            or not SLUG.fullmatch(exclusion_id)
            or exclusion_id in excluded_ids
        ):
            raise BackupError("declared exclusion ID is invalid")
        if category not in EXCLUSION_CATEGORIES:
            raise BackupError("declared exclusion category is unsupported")
        expected_treatment = (
            "logical_dump_only"
            if category == "postgres_physical_database_files"
            else "metadata_count_only"
        )
        if treatment != expected_treatment:
            raise BackupError("declared exclusion treatment does not match its category")
        path_text = relative.as_posix()
        if any(
            path_text == source_path
            or path_text.startswith(source_path + "/")
            or source_path.startswith(path_text + "/")
            for source_path in source_paths
        ):
            raise BackupError("declared exclusion overlaps an application source")
        excluded_ids.add(exclusion_id)
        excluded_categories.add(category)
        path = STATE_DIR.joinpath(*relative.parts)
        _ensure_no_symlink_components(
            path, allow_foreign_leaf=category == "postgres_physical_database_files"
        )
        if category == "postgres_physical_database_files":
            if path_text != "postgres-data":
                raise BackupError(
                    "PostgreSQL physical-data exclusion is outside the fixed task mount"
                )
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise BackupError("PostgreSQL physical data must be an owned task directory")
        else:
            _lstat_owned(path)
    if not REQUIRED_EXCLUSION_CATEGORIES.issubset(excluded_categories):
        raise BackupError("source catalog is missing required excluded-state categories")
    return catalog


def _validate_quiescence(path: Path) -> dict[str, Any]:
    """Requires a fresh owner attestation covering all writers and external effects.

    中文：要求运行 owner 提供新鲜回执，确认所有 writer 与外部副作用已暂停。
    """
    receipt = _read_json(path)
    if receipt.get("format") != QUIESCENCE_FORMAT or receipt.get("status") != "QUIESCENT":
        raise BackupError("a completed task-stack quiescence receipt is required")
    if (
        receipt.get("state_root") != str(STATE_DIR)
        or receipt.get("compose_project") != PROJECT_NAME
    ):
        raise BackupError("quiescence receipt is scoped to another state root or Compose project")
    components = receipt.get("quiescent_components")
    if not isinstance(components, list) or not QUIESCENT_COMPONENTS.issubset(set(components)):
        raise BackupError("quiescence receipt does not cover every required application writer")
    for field in (
        "effects_suspended",
        "outbound_sends_suspended",
        "schedules_suspended",
        "automation_replay_disabled",
    ):
        if receipt.get(field) is not True:
            raise BackupError(
                "quiescence receipt does not suspend effects, sends, schedules, and replay"
            )
    owner = receipt.get("owner")
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", owner):
        raise BackupError("quiescence receipt owner is missing or invalid")
    try:
        created = dt.datetime.fromisoformat(str(receipt.get("created_at")))
        expires = dt.datetime.fromisoformat(str(receipt.get("expires_at")))
    except ValueError as exc:
        raise BackupError("quiescence receipt timestamps are invalid") from exc
    now = _now()
    if (
        created.tzinfo is None
        or expires.tzinfo is None
        or created > now + dt.timedelta(minutes=5)
        or expires <= now
    ):
        raise BackupError("quiescence receipt is not currently valid")
    if expires - created > dt.timedelta(hours=2) or now - created > dt.timedelta(hours=2):
        raise BackupError("quiescence receipt is outside the bounded acceptance window")
    return receipt


def _sqlite_table_counts(
    path: Path, *, allow_exchange_verifier: bool = False
) -> tuple[dict[str, int], dict[str, int]]:
    """Returns table row counts and counts for schema-detected secret tables.

    中文：返回各表行数，并统计按 schema 名称识别的敏感表记录数。
    """
    _validate_sqlite_sidecars(path)
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            connection.execute("BEGIN")
            return _sqlite_connection_counts(
                connection, allow_exchange_verifier=allow_exchange_verifier
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupError("SQLite source could not be read consistently") from exc


def _sqlite_connection_counts(
    connection: sqlite3.Connection,
    *,
    allow_exchange_verifier: bool = False,
) -> tuple[dict[str, int], dict[str, int]]:
    """Counts tables on an existing SQLite snapshot transaction.

    中文：在现有 SQLite snapshot transaction 上统计表行数。
    """
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        raise BackupError("SQLite integrity check failed")
    table_cursor = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    counts: dict[str, int] = {}
    secret_counts: dict[str, int] = {}
    exchange_verifier_seen = False
    for index, (table,) in enumerate(table_cursor, start=1):
        if index > MAX_DATABASE_TABLES:
            raise BackupError("SQLite table inventory exceeds its bounded count")
        if not isinstance(table, str) or "\x00" in table:
            raise BackupError("SQLite contains an unsupported table identifier")
        column_info = connection.execute(f"PRAGMA table_info({_quote_sqlite(table)})").fetchall()
        columns = [str(column[1]) for column in column_info]
        if allow_exchange_verifier and table == "api_credentials":
            _validate_exchange_api_credentials(connection, column_info)
            exchange_verifier_seen = True
            is_sensitive = False
        else:
            is_sensitive = _is_sensitive_schema(table, columns)
        count = int(
            connection.execute(f"SELECT COUNT(*) FROM {_quote_sqlite(table)}").fetchone()[0]
        )
        if is_sensitive:
            secret_counts[table] = count
        else:
            counts[table] = count
    if allow_exchange_verifier and not exchange_verifier_seen:
        raise BackupError("Exchange API credential verifier table is missing")
    return counts, secret_counts


def _validate_sqlite_sidecars(path: Path) -> None:
    """Rejects symlinked or foreign-owned SQLite WAL/SHM sidecars.

    中文：拒绝符号链接或非当前用户所有的 SQLite WAL/SHM sidecar。
    """
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        _ensure_no_symlink_components(sidecar)
        _lstat_owned(sidecar, regular=True)


def _is_sensitive_schema(table: str, columns: list[str]) -> bool:
    """Finds likely credential-value fields while preserving external reference fields.

    中文：识别疑似凭据值字段，同时保留 `_ref`、`_id`、`_uri` 等外部引用字段。
    """
    if any(
        SENSITIVE_NAME.search(column) and not SECRET_REFERENCE_SUFFIX.search(column)
        for column in columns
    ):
        return True
    if not SENSITIVE_TABLE_NAME.search(table):
        return False
    return any(
        column.lower() not in SAFE_CREDENTIAL_METADATA_COLUMNS
        and not SECRET_REFERENCE_SUFFIX.search(column)
        for column in columns
    )


def _quote_sqlite(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _is_approved_exchange_store(resource: dict[str, Any]) -> bool:
    """Limits the verifier exception to the canonical Exchange SQLite resource.

    中文：将 verifier 例外限制在规范 Exchange SQLite source 上。
    """
    categories = resource.get("categories")
    return (
        resource.get("kind") == "sqlite"
        and resource.get("owner") == "exchange"
        and resource.get("path") == EXCHANGE_DATABASE_PATH
        and isinstance(categories, list)
        and "exchange_database" in categories
    )


def _validate_exchange_api_credentials(
    connection: sqlite3.Connection, column_info: list[tuple[Any, ...]]
) -> None:
    """Validates Exchange's exact API-key verifier schema and SHA-256 encoding.

    中文：仅允许规范 api_credentials schema，并校验 token_digest 为 SHA-256 小写十六进制。
    """
    actual_schema = tuple(
        (
            str(column[1]),
            str(column[2]).upper(),
            int(column[3]),
            column[4],
            int(column[5]),
        )
        for column in column_info
    )
    if actual_schema != EXCHANGE_API_CREDENTIAL_SCHEMA:
        raise BackupError(
            "Exchange API credential verifier schema differs from the approved schema"
        )
    unique_indexes: set[tuple[str, ...]] = set()
    for index in connection.execute(f"PRAGMA index_list({_quote_sqlite('api_credentials')})"):
        if int(index[2]) != 1:
            continue
        index_columns = tuple(
            str(row[2])
            for row in connection.execute(f"PRAGMA index_info({_quote_sqlite(str(index[1]))})")
        )
        unique_indexes.add(index_columns)
    if not {("token_digest",), ("api_key_id",)}.issubset(unique_indexes):
        raise BackupError(
            "Exchange API credential verifier indexes differ from the approved schema"
        )
    invalid_digests = connection.execute(
        "SELECT COUNT(*) FROM api_credentials "
        "WHERE typeof(token_digest) != 'text' OR length(token_digest) != 64 "
        "OR token_digest GLOB '*[^0-9a-f]*'"
    ).fetchone()
    if invalid_digests is None or int(invalid_digests[0]) != 0:
        raise BackupError("Exchange API credential verifier format is invalid")


def _sqlite_backup(
    source: Path, destination: Path, *, allow_exchange_verifier: bool = False
) -> dict[str, Any]:
    """Uses SQLite online backup only after proving candidate secret tables are empty.

    中文：先核对敏感 schema；发现任何候选凭据记录时拒绝落盘复制。
    """
    _lstat_owned(source, regular=True)
    _validate_sqlite_sidecars(source)
    if destination.exists() or destination.is_symlink():
        raise BackupError("SQLite staging destination already exists")
    source_uri = f"file:{source.as_posix()}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=30)
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection.execute("BEGIN")
        source_counts, excluded = _sqlite_connection_counts(
            source_connection, allow_exchange_verifier=allow_exchange_verifier
        )
        if any(excluded.values()):
            raise BackupError(
                "SQLite source contains candidate credential rows; move them to external secret storage before backup"
            )
        target_connection = sqlite3.connect(destination)
        source_connection.backup(target_connection, pages=256, sleep=0.01)
        integrity = target_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise BackupError("SQLite online backup did not pass integrity validation")
        target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target_connection.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        raise BackupError("SQLite online backup or secret exclusion failed closed") from exc
    finally:
        if target_connection is not None:
            target_connection.close()
        source_connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{destination}{suffix}")
        try:
            sidecar_info = sidecar.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(sidecar_info.st_mode)
            or not stat.S_ISREG(sidecar_info.st_mode)
            or sidecar_info.st_uid != os.geteuid()
        ):
            raise BackupError("SQLite staging sidecar failed no-follow ownership checks")
        sidecar.unlink()
    os.chmod(destination, 0o600)
    counts, remaining_sensitive = _sqlite_table_counts(
        destination, allow_exchange_verifier=allow_exchange_verifier
    )
    if any(remaining_sensitive.values()):
        raise BackupError("sensitive table data remains in the sanitized SQLite snapshot")
    if counts != source_counts:
        raise BackupError("SQLite online backup table counts do not match the quiesced source")
    result = {"table_counts": counts, "excluded_secret_tables": excluded}
    if allow_exchange_verifier:
        result["approved_verifier_fields"] = {
            "api_credentials.token_digest": {
                "format": "sha256_lowercase_hex",
                "validated_rows": counts["api_credentials"],
            }
        }
    return result


def _secret_file(relative: PurePosixPath) -> bool:
    return _is_secret_path(relative)


def _tree_exclusion_category(relative: PurePosixPath) -> str | None:
    """Classifies non-authoritative tree files by path without reading content.

    中文：只按路径将非权威文件归入凭据、缓存或日志/fixture 分类。
    """
    if _secret_file(relative):
        return "plaintext_credentials_session_tokens_secret_files"
    if REFERENCE_PATH_PART.search("/" + relative.as_posix() + "/"):
        return "models_toolchain_and_cache"
    parts = {part.lower() for part in relative.parts}
    if parts & {
        "evidence",
        "logs",
        "fixtures",
        "fixture",
        "test-fixtures",
        "session-logs",
        "runtime-logs",
    }:
        return "logs_evidence_test_fixtures"
    if relative.suffix.lower() in {".log", ".log.1", ".trace"}:
        return "logs_evidence_test_fixtures"
    return None


def _resource_path(resource: dict[str, Any]) -> Path:
    relative = _safe_relative(resource.get("path"))
    path = STATE_DIR.joinpath(*relative.parts)
    _ensure_no_symlink_components(path)
    return path


def _stage_tree(resource: dict[str, Any], staging: Path) -> dict[str, Any]:
    """Copies a tree with bounded IO, omitting credential files by name only.

    中文：以有界 IO 复制文件树，只按路径名排除凭据文件。
    """
    source_root = _resource_path(resource)
    records: list[dict[str, Any]] = []
    excluded: dict[str, dict[str, int]] = {}
    total_bytes = 0
    for source, info in _walk_no_follow(source_root):
        relative = PurePosixPath(source.relative_to(source_root).as_posix())
        excluded_category = _tree_exclusion_category(relative)
        if excluded_category:
            category = excluded.setdefault(excluded_category, {"files": 0, "bytes": 0})
            category["files"] += 1
            category["bytes"] += info.st_size
            continue
        if len(records) >= MAX_RESOURCE_FILES:
            raise BackupError("tree source exceeds the bounded file-count limit")
        staged_relative = PurePosixPath("payload", resource["id"], "files", *relative.parts)
        destination = staging.joinpath(*staged_relative.parts)
        digest = _copy_regular(source, destination)
        records.append(
            {
                "archive_path": staged_relative.as_posix(),
                "relative_path": relative.as_posix(),
                **digest,
            }
        )
        total_bytes += digest["size_bytes"]
    tree_digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item["relative_path"]):
        tree_digest.update(
            _json_bytes(
                {
                    "relative_path": record["relative_path"],
                    "sha256": record["sha256"],
                    "size_bytes": record["size_bytes"],
                }
            )
        )
    return {
        "files": records,
        "sha256": tree_digest.hexdigest(),
        "file_count": len(records),
        "total_bytes": total_bytes,
        "excluded_categories": excluded,
    }


def _stage_reference(resource: dict[str, Any]) -> dict[str, Any]:
    """Records source metadata and a content digest without copying large assets.

    中文：不复制大型模型、toolchain 或 cache，只记录其文件清单和内容摘要。
    """
    root = _resource_path(resource)
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    skipped = 0
    skipped_bytes = 0
    if root.is_file():
        base = root.parent
        items: Iterator[tuple[Path, os.stat_result]] = iter(
            [(root, root.stat(follow_symlinks=False))]
        )
    else:
        base = root
        items = _walk_no_follow(root)
    for path, info in items:
        relative = PurePosixPath(path.relative_to(base).as_posix())
        if _secret_file(relative):
            skipped += 1
            skipped_bytes += info.st_size
            continue
        file_digest, size = _sha256_file(path)
        digest.update(
            _json_bytes(
                {"relative_path": relative.as_posix(), "sha256": file_digest, "size_bytes": size}
            )
        )
        file_count += 1
        if file_count > MAX_RESOURCE_FILES:
            raise BackupError("reference source exceeds the bounded file-count limit")
        total_bytes += size
    if not file_count:
        raise BackupError("reference source contains no hashable files")
    return {
        "source_sha256": digest.hexdigest(),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "excluded_secret_files": skipped,
        "excluded_secret_bytes": skipped_bytes,
        "copied": False,
    }


def _compose_base() -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        PROJECT_NAME,
        "--file",
        str(COMPOSE_FILE),
    ]


def _compose_env() -> dict[str, str]:
    return {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "CYRENE_AETHER_ACCEPTANCE_STATE": str(STATE_DIR),
    }


def _stop_owned_child(process: subprocess.Popen[bytes]) -> None:
    """Stops and reaps only a subprocess started by this utility.

    中文：只终止并回收本工具自行启动的子进程。
    """
    if process.poll() is not None:
        return
    with contextlib.suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        process.wait()


def _verify_local_docker_engine() -> None:
    """Requires Ubuntu 24.04 WSL2 x86_64 and a local Unix Docker socket.

    中文：仅允许 Ubuntu 24.04 WSL2 x86_64 与本机 Unix Docker socket。
    """
    if platform.machine().lower() != "x86_64":
        raise BackupError("backup and restore require Ubuntu 24.04 WSL2 x86_64")
    release = platform.release().lower()
    if "microsoft" not in release or "wsl2" not in release:
        raise BackupError("backup and restore require Ubuntu 24.04 WSL2 x86_64")
    os_release_path = Path("/etc/os-release")
    if os_release_path.is_symlink():
        try:
            target = os_release_path.resolve(strict=True)
        except OSError as exc:
            raise BackupError("Ubuntu host identity could not be verified") from exc
        if target != Path("/usr/lib/os-release"):
            raise BackupError("Ubuntu host identity could not be verified")
        os_release_path = target
    try:
        os_release = {}
        for line in os_release_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                os_release[key] = value.strip().strip('"')
    except OSError as exc:
        raise BackupError("Ubuntu host identity could not be verified") from exc
    if os_release.get("ID") != "ubuntu" or os_release.get("VERSION_ID") != "24.04":
        raise BackupError("backup and restore require Ubuntu 24.04 WSL2 x86_64")
    try:
        context = subprocess.run(
            ["docker", "context", "show"],
            env=_compose_env(),
            capture_output=True,
            timeout=15,
            check=False,
        )
        if context.returncode != 0:
            raise BackupError("local Docker context could not be verified")
        context_name = context.stdout.decode("ascii", "strict").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", context_name):
            raise BackupError("local Docker context name is invalid")
        inspected = subprocess.run(
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
                context_name,
            ],
            env=_compose_env(),
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        raise BackupError("local Docker context could not be verified") from exc
    if inspected.returncode != 0:
        raise BackupError("local Docker context could not be verified")
    endpoint = inspected.stdout.decode("utf-8", "strict").strip()
    allowed_socket = endpoint in {"unix:///var/run/docker.sock", "unix:///run/docker.sock"}
    allowed_desktop_socket = endpoint.startswith(
        "unix:///mnt/wsl/docker-desktop/shared-sockets/"
    ) and endpoint.endswith("/docker.proxy.sock")
    if not (allowed_socket or allowed_desktop_socket):
        raise BackupError("remote Docker contexts are outside the Aether local-only scope")


def _run_compose(
    args: list[str], *, timeout: int = 120, input_stream: BinaryIO | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Runs only the fixed task Compose project and suppresses secret-bearing output.

    中文：仅操作固定任务 Compose project，并抑制可能含敏感信息的进程输出。
    """
    try:
        return subprocess.run(
            _compose_base() + args,
            env=_compose_env(),
            stdin=input_stream,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("task PostgreSQL command could not complete") from exc


def _verify_postgres_container() -> str:
    """Proves the selected PostgreSQL container belongs to this task and mount.

    中文：确认选中的 PostgreSQL container 与本任务 project 和 bind mount 匹配。
    """
    _verify_local_docker_engine()
    _secure_private_directory(STATE_DIR, create=False)
    postgres_data = STATE_DIR / "postgres-data"
    _ensure_no_symlink_components(postgres_data, allow_foreign_leaf=True)
    postgres_info = postgres_data.lstat()
    if stat.S_ISLNK(postgres_info.st_mode) or not stat.S_ISDIR(postgres_info.st_mode):
        raise BackupError("PostgreSQL data mount is not a real task directory")
    result = _run_compose(["ps", "--quiet", "postgres"])
    if result.returncode != 0:
        raise BackupError("task PostgreSQL service could not be resolved")
    container_id = result.stdout.decode("ascii", "ignore").strip().splitlines()
    if len(container_id) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", container_id[0]):
        raise BackupError("exactly one task PostgreSQL container is required")
    inspected = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{json .Config.Labels}}|||{{json .Mounts}}",
            container_id[0],
        ],
        env=_compose_env(),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if inspected.returncode != 0:
        raise BackupError("task PostgreSQL container identity could not be verified")
    try:
        labels_raw, mounts_raw = inspected.stdout.decode("utf-8").strip().split("|||", 1)
        labels = json.loads(labels_raw)
        mounts = json.loads(mounts_raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupError("task PostgreSQL container identity is invalid") from exc
    expected_source = postgres_data.absolute()
    valid_mount = False
    for mount in mounts:
        if mount.get("Destination") != "/var/lib/postgresql/data" or mount.get("Type") != "bind":
            continue
        mount_source = Path(mount.get("Source", "")).absolute()
        try:
            _ensure_no_symlink_components(mount_source, allow_foreign_leaf=True)
        except BackupError:
            continue
        valid_mount = mount_source == expected_source
        if valid_mount:
            break
    if (
        labels.get("com.docker.compose.project") != PROJECT_NAME
        or labels.get("com.docker.compose.service") != "postgres"
        or not valid_mount
    ):
        raise BackupError(
            "PostgreSQL container is not bound to the exact task-owned data directory"
        )
    return container_id[0]


def _postgres_physical_inventory(container_id: str) -> dict[str, int]:
    """Counts task PostgreSQL files through its verified container, without reading bytes.

    中文：通过已验证的 task container 统计 PostgreSQL 文件数和大小，不读取文件内容。
    """
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            container_id,
            "find",
            "-P",
            "/var/lib/postgresql/data",
            "-type",
            "f",
            "-printf",
            "%s\\n",
        ],
        env=_compose_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    file_count = 0
    total_bytes = 0
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line.isdigit():
                _stop_owned_child(process)
                raise BackupError("PostgreSQL physical-file inventory returned invalid metadata")
            file_count += 1
            total_bytes += int(line)
            if file_count > MAX_TOTAL_PAYLOAD_FILES:
                _stop_owned_child(process)
                raise BackupError("PostgreSQL physical-file inventory exceeds its bounded count")
        _stdout, _stderr = process.communicate(timeout=300)
    except subprocess.TimeoutExpired as exc:
        _stop_owned_child(process)
        raise BackupError("PostgreSQL physical-file inventory timed out") from exc
    except BaseException:
        _stop_owned_child(process)
        raise
    if process.returncode != 0:
        raise BackupError("PostgreSQL physical-file inventory failed")
    return {"files": file_count, "bytes": total_bytes}


def _pg_query(query: str, *, database: str = "cyrene_aether") -> list[str]:
    if not DB_IDENTIFIER.fullmatch(database):
        raise BackupError("PostgreSQL database identifier is invalid")
    result = _run_compose(
        [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-qAt",
            "-U",
            "cyrene_aether",
            "-d",
            database,
            "-c",
            query,
        ]
    )
    if result.returncode != 0:
        raise BackupError("task PostgreSQL metadata query failed")
    return result.stdout.decode("utf-8", "strict").splitlines()


def _pg_database_inventory() -> int:
    """Rejects unlisted application databases and counts prior isolated restores.

    中文：拒绝未登记的应用数据库，并记录此前独立恢复演练的数据库数量。
    """
    databases = _pg_query(
        "SELECT datname FROM pg_database WHERE datistemplate = FALSE "
        "AND datallowconn = TRUE ORDER BY datname"
    )
    if "cyrene_aether" not in databases:
        raise BackupError("the required task PostgreSQL database is missing")
    restore_databases = [name for name in databases if name.startswith("aether_restore_")]
    recognized_restores = {
        name
        for name in restore_databases
        if re.fullmatch(r"aether_restore_[a-f0-9]{10}_[a-f0-9]{6}", name)
    }
    unexpected = set(databases) - {"cyrene_aether", "postgres"} - recognized_restores
    if unexpected:
        raise BackupError("task PostgreSQL contains an unlisted application database")
    if "postgres" in databases:
        user_tables = _pg_query(
            "SELECT COUNT(*) FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog','information_schema')",
            database="postgres",
        )
        if len(user_tables) != 1 or user_tables[0] != "0":
            raise BackupError("the default PostgreSQL database contains unlisted application state")
    return len(recognized_restores)


def _pg_table_inventory() -> tuple[dict[str, int], dict[str, int]]:
    """Counts user tables and identifies secret-bearing tables by schema only.

    中文：只读取 schema 名称与行数，枚举用户表并识别敏感表，不读取业务字段内容。
    """
    rows = _pg_query(
        "SELECT c.table_schema || E'\\t' || c.table_name || E'\\t' || c.column_name "
        "FROM information_schema.columns AS c "
        "WHERE c.table_schema NOT IN ('pg_catalog','information_schema') "
        "AND EXISTS (SELECT 1 FROM information_schema.tables AS t "
        "WHERE t.table_schema=c.table_schema AND t.table_name=c.table_name "
        "AND t.table_type='BASE TABLE') "
        "ORDER BY c.table_schema, c.table_name, c.ordinal_position"
    )
    columns: dict[str, set[str]] = {}
    for row in rows:
        parts = row.split("\t")
        if len(parts) != 3:
            raise BackupError("task PostgreSQL returned an invalid schema inventory")
        schema, table, column = parts
        if not DB_IDENTIFIER.fullmatch(schema) or not DB_IDENTIFIER.fullmatch(table):
            raise BackupError(
                "PostgreSQL table identifiers outside the backup contract are unsupported"
            )
        columns.setdefault(f"{schema}.{table}", set()).add(column)
        if len(columns) > MAX_DATABASE_TABLES:
            raise BackupError("PostgreSQL table inventory exceeds its bounded count")
    included: dict[str, int] = {}
    excluded: dict[str, int] = {}
    for qualified, names in sorted(columns.items()):
        schema, table = qualified.split(".", 1)
        quoted = f'"{schema}"."{table}"'
        count_rows = _pg_query(f"SELECT COUNT(*) FROM {quoted}")
        if len(count_rows) != 1 or not count_rows[0].isdigit():
            raise BackupError("task PostgreSQL table count could not be reconciled")
        count = int(count_rows[0])
        if _is_sensitive_schema(table, list(names)):
            excluded[qualified] = count
        else:
            included[qualified] = count
    return included, excluded


def _pg_dump(destination: Path) -> dict[str, Any]:
    """Creates a custom-format PostgreSQL dump through the owned Compose service.

    中文：通过本任务 Compose service 执行 pg_dump，绝不复制 PostgreSQL 数据目录。
    """
    container_id = _verify_postgres_container()
    physical_inventory = _postgres_physical_inventory(container_id)
    restore_database_count = _pg_database_inventory()
    counts, excluded = _pg_table_inventory()
    reconciled_counts = {**counts, **{table: 0 for table in excluded}}
    args = [
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--dbname=cyrene_aether",
        "--username=cyrene_aether",
    ]
    for qualified in excluded:
        args.append(f"--exclude-table-data={qualified}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            process = subprocess.Popen(
                _compose_base() + args,
                env=_compose_env(),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.PIPE,
            )
            _stdout, _stderr = process.communicate(timeout=3600)
            output.flush()
            os.fsync(output.fileno())
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            _stop_owned_child(process)
        raise BackupError("task PostgreSQL dump timed out; partial staging retained") from exc
    except BaseException:
        if process is not None:
            _stop_owned_child(process)
        raise
    except OSError as exc:
        raise BackupError("task PostgreSQL dump could not start") from exc
    finally:
        os.close(fd)
    if process is None or process.returncode != 0:
        raise BackupError("task PostgreSQL dump failed; diagnostic output was withheld")
    digest, size = _sha256_file(destination)
    _verify_pg_archive(destination)
    return {
        "sha256": digest,
        "size_bytes": size,
        "table_counts": reconciled_counts,
        "excluded_secret_tables": excluded,
        "physical_database_files": physical_inventory,
        "restore_drill_databases": restore_database_count,
        "format": "pg_dump_custom",
    }


def _verify_pg_archive(path: Path) -> None:
    """Checks PostgreSQL custom archive structure without printing its contents.

    中文：验证 PostgreSQL custom archive 结构，不输出其中的对象内容。
    """
    with path.open("rb") as stream:
        result = _run_compose(
            ["exec", "-T", "postgres", "pg_restore", "--list"],
            timeout=300,
            input_stream=stream,
        )
    if result.returncode != 0 or not result.stdout.startswith(b";"):
        raise BackupError("PostgreSQL custom archive validation failed")


def _stage_resource(resource: dict[str, Any], staging: Path) -> dict[str, Any]:
    """Stages one catalog resource and produces reconciliation metadata.

    中文：暂存一项清单资源并生成哈希与计数对账信息。
    """
    kind = resource["kind"]
    record: dict[str, Any] = {
        "id": resource["id"],
        "owner": resource["owner"],
        "categories": sorted(resource["categories"]),
        "kind": kind,
        "secret_policy": resource["secret_policy"],
        "source_revision": resource["source_revision"],
        "source_revision_sha256": hashlib.sha256(resource["source_revision"].encode()).hexdigest(),
    }
    if kind == "reference":
        record["path"] = resource["path"]
        record.update(_stage_reference(resource))
        return record
    if kind == "postgres":
        relative = PurePosixPath("payload", resource["id"], "database.custom")
        destination = staging.joinpath(*relative.parts)
        record.update(_pg_dump(destination))
        record["archive_path"] = relative.as_posix()
        return record
    source = _resource_path(resource)
    record["path"] = resource["path"]
    if kind == "sqlite":
        relative = PurePosixPath("payload", resource["id"], "database.sqlite3")
        destination = staging.joinpath(*relative.parts)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        sanitized = _sqlite_backup(
            source, destination, allow_exchange_verifier=_is_approved_exchange_store(resource)
        )
        digest, size = _sha256_file(destination)
        record.update(sanitized)
        record.update({"archive_path": relative.as_posix(), "sha256": digest, "size_bytes": size})
        return record
    record.update(_stage_tree(resource, staging))
    return record


def _count_declared_exclusions(catalog: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Counts declared excluded files and bytes using metadata only.

    中文：只读取文件系统元数据，统计清单声明的排除文件数与字节数。
    """
    totals = {
        category: {"files": 0, "bytes": 0, "declared_roots": 0}
        for category in REQUIRED_EXCLUSION_CATEGORIES
    }
    for exclusion in catalog["declared_exclusions"]:
        category = exclusion["category"]
        relative = _safe_relative(exclusion["path"])
        path = STATE_DIR.joinpath(*relative.parts)
        _ensure_no_symlink_components(
            path, allow_foreign_leaf=category == "postgres_physical_database_files"
        )
        if category == "postgres_physical_database_files":
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise BackupError("PostgreSQL physical data must be a task directory")
            totals[category]["declared_roots"] += 1
            continue
        info = _lstat_owned(path)
        count = 0
        size = 0
        if stat.S_ISREG(info.st_mode):
            count = 1
            size = info.st_size
        elif stat.S_ISDIR(info.st_mode):
            for _file, file_info in _walk_no_follow(path):
                count += 1
                size += file_info.st_size
                if count > MAX_RESOURCE_FILES:
                    raise BackupError("declared exclusion exceeds the bounded file-count limit")
        else:
            raise BackupError("declared exclusion must be a regular file or directory")
        totals[category]["files"] += count
        totals[category]["bytes"] += size
        totals[category]["declared_roots"] += 1
    return totals


def _preflight_catalog_sources(catalog: dict[str, Any], container_id: str) -> None:
    """Checks every declared store before creating any snapshot payload.

    中文：在创建任何快照 payload 前检查所有已登记 store，避免不完整预检留下备份内容。
    """
    for resource in catalog["sources"]:
        kind = resource["kind"]
        if kind == "sqlite":
            _, sensitive = _sqlite_table_counts(
                _resource_path(resource),
                allow_exchange_verifier=_is_approved_exchange_store(resource),
            )
            if any(sensitive.values()):
                raise BackupError(
                    "SQLite source contains candidate credential rows; move them to external secret storage before backup"
                )
        elif kind in {"tree", "reference"}:
            source = _resource_path(resource)
            info = _lstat_owned(source)
            if stat.S_ISDIR(info.st_mode):
                for count, (_path, _file_info) in enumerate(_walk_no_follow(source), start=1):
                    if count > MAX_RESOURCE_FILES:
                        raise BackupError("source tree exceeds the bounded file-count limit")
            elif not stat.S_ISREG(info.st_mode):
                raise BackupError("tree and reference sources must be regular files or directories")
    _postgres_physical_inventory(container_id)
    _pg_database_inventory()
    _pg_table_inventory()


def _tree_manifest_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for resource in manifest["resources"]:
        if resource["kind"] == "tree":
            files.extend(resource["files"])
        elif resource["kind"] in {"sqlite", "postgres"}:
            files.append(
                {
                    "archive_path": resource["archive_path"],
                    "sha256": resource["sha256"],
                    "size_bytes": resource["size_bytes"],
                }
            )
    return files


def _archive_staging(staging: Path, archive: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Writes a deterministic gzip tar with a manifest and no link entries.

    中文：写入含 manifest 的 gzip tar，并确保归档只包含普通文件。
    """
    manifest_path = staging / MANIFEST_NAME
    _atomic_json(manifest_path, manifest)
    archive_tmp = archive.parent.parent / f".{archive.parent.name}.tar.gz.partial"
    fd = os.open(
        archive_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        with os.fdopen(fd, "wb", closefd=False) as raw:
            with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as tar:
                payload_root = staging / "payload"
                archive_sources = [manifest_path]
                if payload_root.exists():
                    archive_sources.extend(
                        path for path in payload_root.rglob("*") if path.is_file()
                    )
                for path in sorted(
                    archive_sources, key=lambda item: item.relative_to(staging).as_posix()
                ):
                    relative = PurePosixPath(path.relative_to(staging).as_posix())
                    info = _lstat_owned(path, regular=True)
                    archive_info = tarfile.TarInfo(relative.as_posix())
                    archive_info.size = info.st_size
                    archive_info.mode = 0o600
                    archive_info.uid = 0
                    archive_info.gid = 0
                    archive_info.uname = ""
                    archive_info.gname = ""
                    archive_info.mtime = 0
                    source_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        with os.fdopen(source_fd, "rb", closefd=False) as stream:
                            tar.addfile(archive_info, stream)
                    finally:
                        os.close(source_fd)
            raw.flush()
            os.fsync(raw.fileno())
    finally:
        os.close(fd)
    os.replace(archive_tmp, archive)
    _fsync_directory(archive.parent)
    digest, size = _sha256_file(archive)
    return {"archive_sha256": digest, "archive_size_bytes": size}


def _write_journal(
    path: Path, *, snapshot_id: str, state: str, completed: list[str], detail: str
) -> None:
    _atomic_json(
        path,
        {
            "format": "cyrene.aether.backup-journal.v1",
            "snapshot_id": snapshot_id,
            "state": state,
            "completed_resource_ids": completed,
            "updated_at": _iso(_now()),
            "detail": detail,
        },
    )


def cmd_backup(args: argparse.Namespace) -> int:
    """Creates one complete, quiesced, atomically published snapshot.

    中文：在全栈 quiescence 回执有效时创建完整快照，并原子发布结果。
    """
    catalog_path = Path(args.catalog).absolute()
    catalog = _validate_catalog(_read_json(catalog_path))
    quiescence = _validate_quiescence(Path(args.quiescence_receipt).absolute())
    _verify_local_docker_engine()
    container_id = _verify_postgres_container()
    _preflight_catalog_sources(catalog, container_id)
    declared_exclusions = _count_declared_exclusions(catalog)
    output_dir = Path(args.output_dir).absolute()
    if output_dir == STATE_DIR or output_dir.is_relative_to(STATE_DIR):
        raise BackupError("snapshot output must be outside the live task state root")
    if STATE_DIR.is_relative_to(output_dir):
        raise BackupError("snapshot output may not contain the live task state root")
    _secure_private_directory(output_dir, create=True)
    snapshot_id = args.snapshot_id or _now().strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", snapshot_id):
        raise BackupError("snapshot ID must use the generated timestamp and random suffix format")
    final_dir = output_dir / snapshot_id
    staging = output_dir / f".{snapshot_id}.partial"
    if final_dir.exists() or final_dir.is_symlink() or staging.exists() or staging.is_symlink():
        raise BackupError("snapshot ID already exists; partial snapshots are retained for review")
    staging.mkdir(mode=0o700)
    _fsync_directory(output_dir)
    journal = staging / "journal.json"
    completed: list[str] = []
    payload_files_staged = 0
    _write_journal(
        journal, snapshot_id=snapshot_id, state="IN_PROGRESS", completed=completed, detail="started"
    )
    tree_exclusions = {
        "plaintext_credentials_session_tokens_secret_files": {"files": 0, "bytes": 0},
        "logs_evidence_test_fixtures": {"files": 0, "bytes": 0},
        "models_toolchain_and_cache": {"files": 0, "bytes": 0},
    }
    resources: list[dict[str, Any]] = []
    try:
        for resource in catalog["sources"]:
            staged = _stage_resource(resource, staging)
            resources.append(staged)
            if staged["kind"] in {"tree", "sqlite", "postgres"}:
                payload_files_staged += int(staged.get("file_count", 1))
                if payload_files_staged > MAX_TOTAL_PAYLOAD_FILES:
                    raise BackupError("snapshot exceeds the bounded total-file limit")
            for category, count in staged.get("excluded_categories", {}).items():
                for key in ("files", "bytes"):
                    tree_exclusions[category][key] += int(count[key])
            if staged["kind"] == "reference":
                tree_exclusions["plaintext_credentials_session_tokens_secret_files"]["files"] += (
                    int(staged.get("excluded_secret_files", 0))
                )
                tree_exclusions["plaintext_credentials_session_tokens_secret_files"]["bytes"] += (
                    int(staged.get("excluded_secret_bytes", 0))
                )
            completed.append(resource["id"])
            _write_journal(
                journal,
                snapshot_id=snapshot_id,
                state="IN_PROGRESS",
                completed=completed,
                detail="resource staged",
            )
        references = [item for item in resources if item["kind"] == "reference"]
        secret_tables = sum(len(item.get("excluded_secret_tables", {})) for item in resources)
        secret_rows = sum(
            sum(item.get("excluded_secret_tables", {}).values()) for item in resources
        )
        secret_exclusions = declared_exclusions["plaintext_credentials_session_tokens_secret_files"]
        for key in ("files", "bytes"):
            secret_exclusions[key] += tree_exclusions[
                "plaintext_credentials_session_tokens_secret_files"
            ][key]
        logs_exclusions = declared_exclusions["logs_evidence_test_fixtures"]
        for key in ("files", "bytes"):
            logs_exclusions[key] += tree_exclusions["logs_evidence_test_fixtures"][key]
        cache_exclusions = tree_exclusions["models_toolchain_and_cache"]
        pg_resource = next(item for item in resources if item["kind"] == "postgres")
        pg_physical = {
            **declared_exclusions["postgres_physical_database_files"],
            **pg_resource.get("physical_database_files", {}),
        }
        manifest: dict[str, Any] = {
            "format": SNAPSHOT_FORMAT,
            "status": "COMPLETE",
            "snapshot_id": snapshot_id,
            "created_at": _iso(_now()),
            "state_root_label": STATE_DIR.name,
            "compose_project": PROJECT_NAME,
            "source_catalog_sha256": _catalog_digest(catalog),
            "source_revisions": catalog["source_revisions"],
            "source_version_sha256": hashlib.sha256(
                _json_bytes(catalog["source_revisions"])
            ).hexdigest(),
            "quiescence_receipt_sha256": hashlib.sha256(
                _secure_input_file(
                    Path(args.quiescence_receipt).absolute(), max_bytes=MAX_CATALOG_BYTES
                )
            ).hexdigest(),
            "quiescence_owner": quiescence["owner"],
            "resources": resources,
            "required_categories": sorted(REQUIRED_CATEGORIES),
            "external_secret_refs": catalog["external_secret_refs"],
            "excluded_categories": {
                "plaintext_credentials_session_tokens_secret_files": {
                    **secret_exclusions,
                    "database_tables": secret_tables,
                    "database_rows": secret_rows,
                    "copied": 0,
                },
                "postgres_physical_database_files": {
                    **pg_physical,
                    "copied": 0,
                    "replacement": "pg_dump_custom",
                },
                "postgres_restore_drill_databases": {
                    "databases": int(pg_resource.get("restore_drill_databases", 0)),
                    "copied": 0,
                    "reason": "prior fresh-restore namespaces are retained outside the active source database",
                },
                "models_toolchain_and_cache": {
                    "copied": 0,
                    "references": len(references),
                    "files_hashed": sum(item.get("file_count", 0) for item in references),
                    "bytes_hashed": sum(item.get("total_bytes", 0) for item in references),
                    "excluded_tree_files": cache_exclusions["files"],
                    "excluded_tree_bytes": cache_exclusions["bytes"],
                },
                "logs_evidence_test_fixtures": {
                    **logs_exclusions,
                    "copied": 0,
                },
            },
            "restore_policy": {
                "requires_new_empty_target": True,
                "effects_suspended": True,
                "outbound_sends_suspended": True,
                "schedules_suspended": True,
                "automation_replay_disabled": True,
                "historical_cutover_performed": False,
            },
        }
        total_payload_files = sum(
            item.get("file_count", 1)
            for item in resources
            if item["kind"] in {"tree", "sqlite", "postgres"}
        )
        if total_payload_files > MAX_TOTAL_PAYLOAD_FILES:
            raise BackupError("snapshot exceeds the bounded total-file limit")
        if len(_json_bytes(manifest)) > MAX_MANIFEST_BYTES:
            raise BackupError("snapshot manifest exceeds the supported bounded size")
        archive = staging / "snapshot.tar.gz"
        archive_summary = _archive_staging(staging, archive, manifest)
        # The archive cannot contain its own outer receipt, so the receipt binds the complete bytes.
        receipt = {
            "format": "cyrene.aether.backup-receipt.v1",
            "snapshot_id": snapshot_id,
            "archive": archive.name,
            **archive_summary,
            "manifest_sha256": hashlib.sha256(_json_bytes(manifest)).hexdigest(),
            "completed_at": _iso(_now()),
        }
        _atomic_json(staging / RECEIPT_NAME, receipt)
        _write_journal(
            journal,
            snapshot_id=snapshot_id,
            state="COMPLETE",
            completed=completed,
            detail="published",
        )
        # Keep the journal and receipt outside the tar; they remain auditable after publication.
        _fsync_directory(staging)
        os.replace(staging, final_dir)
        _fsync_directory(output_dir)
    except BaseException as original_error:
        try:
            _write_journal(
                journal,
                snapshot_id=snapshot_id,
                state="INTERRUPTED",
                completed=completed,
                detail="incomplete; retained for review",
            )
        except (OSError, BackupError):
            original_error.add_note(
                "The interrupted-run journal could not be updated; inspect the private partial directory."
            )
        raise
    print(f"snapshot_id={snapshot_id} status=COMPLETE resources={len(resources)}")
    print(f"snapshot_path={final_dir}")
    print(f"archive_sha256={archive_summary['archive_sha256']}")
    print(
        "excluded_secret_files="
        f"{secret_exclusions['files']} excluded_secret_tables={secret_tables} "
        f"postgres_physical_files={pg_physical['files']}"
    )
    return 0


def _validate_archive_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or "\\" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise BackupError("archive contains path traversal or an unsupported path")
    return path


def _read_snapshot(snapshot_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Loads and verifies the private snapshot receipt and outer archive digest.

    中文：读取并校验私有快照回执与归档摘要。
    """
    _secure_private_directory(snapshot_dir, create=False)
    receipt = _read_json(snapshot_dir / RECEIPT_NAME)
    if receipt.get("format") != "cyrene.aether.backup-receipt.v1":
        raise BackupError("snapshot receipt format is unsupported")
    journal = _read_json(snapshot_dir / "journal.json")
    if journal.get("state") != "COMPLETE" or journal.get("snapshot_id") != receipt.get(
        "snapshot_id"
    ):
        raise BackupError("snapshot journal is incomplete or does not match its receipt")
    archive_name = receipt.get("archive")
    if archive_name != "snapshot.tar.gz":
        raise BackupError("snapshot archive name is outside the fixed format")
    archive = snapshot_dir / archive_name
    digest, size = _sha256_file(archive)
    if digest != receipt.get("archive_sha256") or size != receipt.get("archive_size_bytes"):
        raise BackupError("snapshot archive digest or size does not match its receipt")
    manifest = _read_archive_manifest(archive)
    if manifest.get("snapshot_id") != receipt.get("snapshot_id"):
        raise BackupError("snapshot receipt and manifest identify different snapshots")
    if hashlib.sha256(_json_bytes(manifest)).hexdigest() != receipt.get("manifest_sha256"):
        raise BackupError("snapshot manifest digest does not match its receipt")
    return archive, manifest, receipt


def _read_archive_manifest(archive: Path) -> dict[str, Any]:
    """Reads a bounded manifest from an archive after checking member safety.

    中文：在确认成员安全后读取归档中的有界 manifest。
    """
    try:
        with gzip.open(archive, mode="rb") as compressed:
            while compressed.read(CHUNK_SIZE):
                pass
        with tarfile.open(archive, mode="r:gz") as tar:
            seen: set[str] = set()
            manifest_data: bytes | None = None
            for member in tar:
                _validate_archive_member(member.name)
                if member.name in seen or not member.isfile():
                    raise BackupError("archive contains duplicate, linked, or special members")
                seen.add(member.name)
                if member.name == MANIFEST_NAME:
                    if member.size > MAX_MANIFEST_BYTES:
                        raise BackupError("snapshot manifest exceeds the supported size")
                    stream = tar.extractfile(member)
                    if stream is None:
                        raise BackupError("snapshot manifest could not be read")
                    manifest_data = stream.read(MAX_MANIFEST_BYTES + 1)
                    if len(manifest_data) != member.size or len(manifest_data) > MAX_MANIFEST_BYTES:
                        raise BackupError("snapshot manifest is truncated")
            if manifest_data is None:
                raise BackupError("snapshot archive has no manifest")
    except (tarfile.TarError, gzip.BadGzipFile, OSError, EOFError) as exc:
        raise BackupError("snapshot archive is truncated or corrupt") from exc
    try:
        manifest = json.loads(manifest_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("snapshot manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != SNAPSHOT_FORMAT:
        raise BackupError("snapshot format is unsupported")
    if manifest.get("status") != "COMPLETE" or manifest.get("compose_project") != PROJECT_NAME:
        raise BackupError("snapshot is incomplete or belongs to another task project")
    _validate_snapshot_manifest(manifest)
    return manifest


def _validate_snapshot_manifest(manifest: dict[str, Any]) -> None:
    """Validates manifest paths, category coverage, digests, and resource records.

    中文：验证 manifest 中的路径、分类覆盖、摘要格式和资源记录结构。
    """
    snapshot_id = manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not re.fullmatch(
        r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{12}", snapshot_id
    ):
        raise BackupError("snapshot ID is invalid")
    if manifest.get("state_root_label") != STATE_DIR.name:
        raise BackupError("snapshot belongs to another task state root")
    revisions = manifest.get("source_revisions")
    required_revisions = {"navigator", "exchange", "plugins", "workspace"}
    if not isinstance(revisions, dict) or not required_revisions.issubset(revisions):
        raise BackupError("snapshot source revisions are incomplete")
    if any(
        not isinstance(revisions[key], str) or not COMMIT.fullmatch(revisions[key])
        for key in required_revisions
    ):
        raise BackupError("snapshot source revision is invalid")
    resources = manifest.get("resources")
    if not isinstance(resources, list) or not 1 <= len(resources) <= MAX_RESOURCES:
        raise BackupError("snapshot resource inventory is invalid")
    covered: set[str] = set()
    identifiers: set[str] = set()
    resources_by_path: dict[str, dict[str, Any]] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            raise BackupError("snapshot resource record is invalid")
        resource_id = resource.get("id")
        kind = resource.get("kind")
        categories = resource.get("categories")
        owner = resource.get("owner")
        if (
            not isinstance(resource_id, str)
            or not SLUG.fullmatch(resource_id)
            or resource_id in identifiers
        ):
            raise BackupError("snapshot resource ID is invalid")
        identifiers.add(resource_id)
        if kind not in {"sqlite", "tree", "postgres", "reference"}:
            raise BackupError("snapshot resource kind is invalid")
        if owner not in OWNER_NAMES:
            raise BackupError("snapshot resource owner is invalid")
        secret_policy = resource.get("secret_policy")
        approved_exchange_store = _is_approved_exchange_store(resource)
        expected_secret_policy = (
            EXCHANGE_SECRET_POLICY if approved_exchange_store else "external_refs_only"
        )
        if secret_policy != expected_secret_policy:
            raise BackupError("snapshot source credential policy is invalid")
        if not isinstance(categories, list) or not categories:
            raise BackupError("snapshot resource categories are invalid")
        for category in categories:
            if category not in CATEGORY_KINDS or kind not in CATEGORY_KINDS[category]:
                raise BackupError("snapshot resource category and kind do not match")
            if category in REQUIRED_CATEGORIES:
                covered.add(category)
        if not isinstance(resource.get("source_revision"), str) or not COMMIT.fullmatch(
            resource["source_revision"]
        ):
            raise BackupError("snapshot resource source revision is invalid")
        if not isinstance(resource.get("source_revision_sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", resource["source_revision_sha256"]
        ):
            raise BackupError("snapshot resource source-version digest is invalid")
        if kind == "reference":
            relative = _safe_relative(resource.get("path"))
            resources_by_path[relative.as_posix()] = resource
            if resource.get("copied") is not False:
                raise BackupError("reference resources must not be copied into the archive")
            if not isinstance(resource.get("file_count"), int) or resource["file_count"] < 1:
                raise BackupError("reference file count is invalid")
            continue
        counts = resource.get("table_counts")
        if kind in {"sqlite", "postgres"} and (
            not isinstance(counts, dict)
            or any(
                not isinstance(name, str) or not isinstance(count, int) or count < 0
                for name, count in counts.items()
            )
        ):
            raise BackupError("snapshot database table counts are invalid")
        if kind == "postgres":
            if resource.get("archive_path") != f"payload/{resource_id}/database.custom":
                raise BackupError("PostgreSQL archive path is outside the fixed resource mapping")
        elif kind == "sqlite":
            relative = _safe_relative(resource.get("path"))
            resources_by_path[relative.as_posix()] = resource
            if resource.get("archive_path") != f"payload/{resource_id}/database.sqlite3":
                raise BackupError("SQLite archive path is outside the fixed resource mapping")
            if relative.as_posix() == EXCHANGE_DATABASE_PATH and not _is_approved_exchange_store(
                resource
            ):
                raise BackupError("Exchange database path is bound to the wrong canonical owner")
            if _is_approved_exchange_store(resource):
                expected_verifiers = {
                    "api_credentials.token_digest": {
                        "format": "sha256_lowercase_hex",
                        "validated_rows": counts.get("api_credentials"),
                    }
                }
                if resource.get("approved_verifier_fields") != expected_verifiers:
                    raise BackupError(
                        "Exchange API credential verifier validation receipt is invalid"
                    )
            elif resource.get("approved_verifier_fields"):
                raise BackupError("non-Exchange resource contains an unapproved verifier exception")
        else:
            relative = _safe_relative(resource.get("path"))
            resources_by_path[relative.as_posix()] = resource
            files = resource.get("files")
            if not isinstance(files, list) or len(files) > MAX_RESOURCE_FILES:
                raise BackupError("snapshot tree file inventory is invalid")
            if not isinstance(resource.get("file_count"), int) or resource["file_count"] != len(
                files
            ):
                raise BackupError("snapshot tree file count is invalid")
            for record in files:
                if not isinstance(record, dict):
                    raise BackupError("snapshot tree file record is invalid")
                _safe_relative(record.get("relative_path"))
                expected_prefix = f"payload/{resource_id}/files/"
                if not isinstance(record.get("archive_path"), str) or not record[
                    "archive_path"
                ].startswith(expected_prefix):
                    raise BackupError("snapshot tree archive path is outside the resource mapping")
                if not isinstance(record.get("sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", record["sha256"]
                ):
                    raise BackupError("snapshot tree file digest is invalid")
                if not isinstance(record.get("size_bytes"), int) or record["size_bytes"] < 0:
                    raise BackupError("snapshot tree file size is invalid")
            if len({record["archive_path"] for record in files}) != len(files):
                raise BackupError("snapshot tree archive paths are duplicated")
            if len({record["relative_path"] for record in files}) != len(files):
                raise BackupError("snapshot tree relative paths are duplicated")
            tree_digest = hashlib.sha256()
            for record in sorted(files, key=lambda item: item["relative_path"]):
                tree_digest.update(
                    _json_bytes(
                        {
                            "relative_path": record["relative_path"],
                            "sha256": record["sha256"],
                            "size_bytes": record["size_bytes"],
                        }
                    )
                )
            if resource.get("sha256") != tree_digest.hexdigest():
                raise BackupError("tree content digest does not reconcile with its file manifest")
        for key in ("sha256",):
            if not isinstance(resource.get(key), str) or not re.fullmatch(
                r"[0-9a-f]{64}", resource[key]
            ):
                raise BackupError("snapshot resource digest is invalid")
        if kind in {"sqlite", "postgres"}:
            if not isinstance(resource.get("size_bytes"), int) or resource["size_bytes"] < 0:
                raise BackupError("snapshot database resource size is invalid")
        elif not isinstance(resource.get("total_bytes"), int) or resource["total_bytes"] < 0:
            raise BackupError("snapshot tree byte count is invalid")
    if REQUIRED_CATEGORIES - covered:
        raise BackupError("snapshot does not cover every required operational-state category")
    for path_text, (required_kind, required_category) in REQUIRED_RESOURCE_PATHS.items():
        resource = resources_by_path.get(path_text)
        if (
            resource is None
            or resource.get("kind") != required_kind
            or required_category not in resource.get("categories", [])
        ):
            raise BackupError("snapshot omits a canonical Aether application store")
    if manifest.get("required_categories") != sorted(REQUIRED_CATEGORIES):
        raise BackupError("snapshot required-category contract differs from this tool version")
    refs = manifest.get("external_secret_refs")
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(ref, str) or not ENV_REF.fullmatch(ref) for ref in refs)
    ):
        raise BackupError("snapshot external secret references are missing or invalid")
    exclusions = manifest.get("excluded_categories")
    required_exclusions = (
        REQUIRED_EXCLUSION_CATEGORIES
        | MANIFEST_EXTRA_EXCLUSION_CATEGORIES
        | {"models_toolchain_and_cache"}
    )
    if not isinstance(exclusions, dict) or not required_exclusions.issubset(exclusions):
        raise BackupError("snapshot exclusion categories or counts are incomplete")
    for category in (
        "plaintext_credentials_session_tokens_secret_files",
        "logs_evidence_test_fixtures",
    ):
        item = exclusions[category]
        if (
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(key), int) or item[key] < 0
                for key in ("files", "bytes", "declared_roots")
            )
            or item.get("copied") != 0
        ):
            raise BackupError("snapshot excluded-file counts are invalid")
    postgres_exclusion = exclusions["postgres_physical_database_files"]
    if (
        not isinstance(postgres_exclusion, dict)
        or any(
            not isinstance(postgres_exclusion.get(key), int) or postgres_exclusion[key] < 0
            for key in ("files", "bytes", "declared_roots")
        )
        or postgres_exclusion.get("copied") != 0
        or postgres_exclusion.get("replacement") != "pg_dump_custom"
    ):
        raise BackupError("snapshot PostgreSQL physical-file exclusion is invalid")
    restore_databases = exclusions["postgres_restore_drill_databases"]
    if (
        not isinstance(restore_databases, dict)
        or not isinstance(restore_databases.get("databases"), int)
        or restore_databases["databases"] < 0
        or restore_databases.get("copied") != 0
    ):
        raise BackupError("snapshot restore-drill database exclusion is invalid")
    reference_exclusion = exclusions["models_toolchain_and_cache"]
    if (
        not isinstance(reference_exclusion, dict)
        or reference_exclusion.get("copied") != 0
        or any(
            not isinstance(reference_exclusion.get(key), int) or reference_exclusion[key] < 0
            for key in (
                "references",
                "files_hashed",
                "bytes_hashed",
                "excluded_tree_files",
                "excluded_tree_bytes",
            )
        )
    ):
        raise BackupError("snapshot model/toolchain/cache reference counts are invalid")
    policy = manifest.get("restore_policy")
    if (
        not isinstance(policy, dict)
        or any(
            policy.get(key) is not True
            for key in (
                "requires_new_empty_target",
                "effects_suspended",
                "outbound_sends_suspended",
                "schedules_suspended",
                "automation_replay_disabled",
            )
        )
        or policy.get("historical_cutover_performed") is not False
    ):
        raise BackupError("snapshot restore policy is unsafe or incomplete")


def _validate_manifest_payloads(archive: Path, manifest: dict[str, Any]) -> None:
    """Reconciles every archive member hash, size, and resource table count.

    中文：逐项核对归档成员的摘要、大小与资源表行数。
    """
    expected_files = {record["archive_path"]: record for record in _tree_manifest_files(manifest)}
    expected_names = set(expected_files) | {MANIFEST_NAME}
    seen: set[str] = set()
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            for member in tar:
                _validate_archive_member(member.name)
                if member.name in seen:
                    raise BackupError("snapshot archive contains duplicate paths")
                seen.add(member.name)
                if not member.isfile() or member.name not in expected_names:
                    raise BackupError("snapshot archive contains an unlisted or unsupported member")
                if member.name == MANIFEST_NAME:
                    continue
                expected = expected_files[member.name]
                if member.size != expected["size_bytes"]:
                    raise BackupError("snapshot archive member size does not match the manifest")
                stream = tar.extractfile(member)
                if stream is None:
                    raise BackupError("snapshot archive member cannot be read")
                digest = hashlib.sha256()
                size = 0
                while chunk := stream.read(CHUNK_SIZE):
                    digest.update(chunk)
                    size += len(chunk)
                if size != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
                    raise BackupError("snapshot archive member digest does not match the manifest")
            if seen != expected_names:
                raise BackupError("snapshot archive is missing one or more manifest members")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError("snapshot archive is truncated or corrupt") from exc
    for resource in manifest["resources"]:
        if resource["kind"] == "sqlite":
            _verify_sqlite_payload(archive, resource)
        elif resource["kind"] == "tree":
            files = resource.get("files", [])
            if not isinstance(files, list) or len(files) != resource.get("file_count"):
                raise BackupError("tree file count does not reconcile with its manifest")
            if sum(item.get("size_bytes", -1) for item in files) != resource.get("total_bytes"):
                raise BackupError("tree byte count does not reconcile with its manifest")


def _verify_sqlite_payload(archive: Path, resource: dict[str, Any]) -> None:
    """Checks SQLite archive integrity and exact included table counts.

    中文：验证 SQLite payload 完整性，并对账所有已包含表的行数。
    """
    with tempfile.TemporaryDirectory(prefix="aether-sqlite-verify-") as temp_dir:
        temporary = Path(temp_dir) / "database.sqlite3"
        os.chmod(temp_dir, 0o700)
        with tarfile.open(archive, mode="r:gz") as tar:
            member = tar.getmember(resource["archive_path"])
            stream = tar.extractfile(member)
            if stream is None:
                raise BackupError("SQLite payload cannot be read")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=False) as output:
                    digest, size = _copy_stream(stream, output)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                os.close(fd)
        if digest != resource["sha256"] or size != resource["size_bytes"]:
            raise BackupError("SQLite payload digest does not match the resource manifest")
        counts, sensitive = _sqlite_table_counts(
            temporary, allow_exchange_verifier=_is_approved_exchange_store(resource)
        )
        if counts != resource.get("table_counts") or any(sensitive.values()):
            raise BackupError("SQLite table counts do not reconcile with the snapshot manifest")


def cmd_verify(args: argparse.Namespace) -> int:
    """Verifies archive integrity, file hashes, SQLite integrity, and counts.

    中文：验证归档完整性、文件摘要、SQLite 完整性和表计数。
    """
    archive, manifest, receipt = _read_snapshot(Path(args.snapshot).absolute())
    _validate_manifest_payloads(archive, manifest)
    print(
        f"snapshot_id={manifest['snapshot_id']} status=VERIFIED "
        f"resources={len(manifest['resources'])} archive_sha256={receipt['archive_sha256']}"
    )
    return 0


def _restore_target_path(target_root: Path, relative: str) -> Path:
    """Maps one validated archive path below a newly created state root.

    中文：将经过验证的归档路径映射到新建状态根目录内。
    """
    safe = _safe_relative(relative)
    target = target_root.joinpath(*safe.parts)
    if not target.absolute().is_relative_to(target_root.absolute()):
        raise BackupError("restore path escapes the new target root")
    return target


def _extract_member(
    archive: tarfile.TarFile, member_name: str, target: Path, expected: dict[str, Any]
) -> None:
    _validate_archive_member(member_name)
    member = archive.getmember(member_name)
    if not member.isfile() or member.size != expected["size_bytes"]:
        raise BackupError("restore member has an invalid type or size")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ensure_no_symlink_components(target.parent)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    stream = archive.extractfile(member)
    if stream is None:
        os.close(fd)
        raise BackupError("restore member cannot be read")
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            digest, size = _copy_stream(stream, output)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(fd)
    if digest != expected["sha256"] or size != expected["size_bytes"]:
        raise BackupError("restore member digest does not match the snapshot manifest")


def _pg_database_exists(database: str) -> bool:
    if not DB_IDENTIFIER.fullmatch(database):
        raise BackupError("PostgreSQL target database identifier is invalid")
    rows = _pg_query(
        "SELECT 1 FROM pg_database WHERE datname = " + "'" + database.replace("'", "''") + "'"
    )
    return bool(rows)


def _pg_restore(archive_path: Path, database: str, expected_counts: dict[str, int]) -> None:
    """Restores a dump into a newly created task-local database namespace.

    中文：将 pg_dump 恢复到新建的本地 task 数据库命名空间。
    """
    _verify_postgres_container()
    if _pg_database_exists(database):
        raise BackupError("PostgreSQL restore database already exists; refusing overwrite")
    create = _run_compose(
        [
            "exec",
            "-T",
            "postgres",
            "createdb",
            "-U",
            "cyrene_aether",
            "-O",
            "cyrene_aether",
            database,
        ]
    )
    if create.returncode != 0:
        raise BackupError("fresh PostgreSQL restore database could not be created")
    with archive_path.open("rb") as stream:
        restored = _run_compose(
            [
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                database,
            ],
            timeout=3600,
            input_stream=stream,
        )
    if restored.returncode != 0:
        raise BackupError("PostgreSQL restore failed; the new database is retained for inspection")
    actual = _pg_table_counts(database)
    if actual != expected_counts:
        raise BackupError("PostgreSQL restored table counts do not reconcile")


def _pg_table_counts(database: str) -> dict[str, int]:
    if not DB_IDENTIFIER.fullmatch(database):
        raise BackupError("PostgreSQL database identifier is invalid")
    list_result = _run_compose(
        [
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-qAt",
            "-U",
            "cyrene_aether",
            "-d",
            database,
            "-c",
            (
                "SELECT table_schema || E'\\t' || table_name FROM information_schema.tables "
                "WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema') "
                "ORDER BY table_schema, table_name"
            ),
        ]
    )
    if list_result.returncode != 0:
        raise BackupError("PostgreSQL restored schema could not be enumerated")
    counts: dict[str, int] = {}
    for row in list_result.stdout.decode("utf-8", "strict").splitlines():
        schema, separator, table = row.partition("\t")
        if (
            not separator
            or not DB_IDENTIFIER.fullmatch(schema)
            or not DB_IDENTIFIER.fullmatch(table)
        ):
            raise BackupError("PostgreSQL restored table identifier is unsupported")
        quoted = f'"{schema}"."{table}"'
        result = _run_compose(
            [
                "exec",
                "-T",
                "postgres",
                "psql",
                "-X",
                "-qAt",
                "-U",
                "cyrene_aether",
                "-d",
                database,
                "-c",
                f"SELECT COUNT(*) FROM {quoted}",
            ]
        )
        if result.returncode != 0:
            raise BackupError("PostgreSQL restored table count could not be reconciled")
        value = result.stdout.decode("ascii", "strict").strip()
        if not value.isdigit():
            raise BackupError("PostgreSQL restored table count is invalid")
        counts[f"{schema}.{table}"] = int(value)
    return counts


def cmd_restore(args: argparse.Namespace) -> int:
    """Restores only into a new private target and a fresh PostgreSQL database.

    中文：只恢复到全新私有目标目录和全新的 PostgreSQL 数据库，不启用任务副作用。
    """
    snapshot_dir = Path(args.snapshot).absolute()
    archive_path, manifest, receipt = _read_snapshot(snapshot_dir)
    _validate_manifest_payloads(archive_path, manifest)
    quiescence = _validate_quiescence(Path(args.quiescence_receipt).absolute())
    _verify_local_docker_engine()
    _verify_postgres_container()
    target = Path(args.target_root).absolute()
    if target.parent != STATE_DIR.parent or not target.name.startswith(
        "cyrene-aether-wsl-restore-"
    ):
        raise BackupError("restore target must be a new sibling of the fixed task state root")
    if target.exists() or target.is_symlink():
        raise BackupError("restore target must not exist; existing state is never overwritten")
    _ensure_no_symlink_components(target.parent)
    _lstat_owned(target.parent, directory=True)
    target.mkdir(mode=0o700)
    _fsync_directory(target.parent)
    target_pg_database = (
        "aether_restore_"
        + re.sub("[^a-f0-9]", "", manifest["snapshot_id"])[-10:]
        + "_"
        + uuid.uuid4().hex[:6]
    )
    restore_receipt_path = target / "restore-receipt.json"
    restore_receipt: dict[str, Any] = {
        "format": RESTORE_FORMAT,
        "restore_id": uuid.uuid4().hex,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_archive_sha256": receipt["archive_sha256"],
        "source_catalog_sha256": manifest["source_catalog_sha256"],
        "quiescence_receipt_sha256": hashlib.sha256(
            _secure_input_file(
                Path(args.quiescence_receipt).absolute(), max_bytes=MAX_CATALOG_BYTES
            )
        ).hexdigest(),
        "quiescence_owner": quiescence["owner"],
        "target_root_label": target.name,
        "target_postgres_database": target_pg_database,
        "status": "IN_PROGRESS",
        "effects_suspended": True,
        "outbound_sends_suspended": True,
        "schedules_suspended": True,
        "automation_replay_disabled": True,
        "historical_cutover_performed": False,
        "created_at": _iso(_now()),
    }
    _atomic_json(restore_receipt_path, restore_receipt)
    _atomic_json(
        target / "RESTORE_SUSPENDED.json",
        {
            "status": "SUSPENDED",
            "effects_enabled": False,
            "outbound_sends_enabled": False,
            "schedules_enabled": False,
            "automation_replay_enabled": False,
            "instruction": "Do not start application workers from this restore target.",
        },
    )
    try:
        with tarfile.open(archive_path, mode="r:gz") as tar:
            for resource in manifest["resources"]:
                if resource["kind"] == "reference":
                    continue
                if resource["kind"] == "postgres":
                    temporary = target / f".{resource['id']}.database.custom"
                    _extract_member(
                        tar,
                        resource["archive_path"],
                        temporary,
                        {"sha256": resource["sha256"], "size_bytes": resource["size_bytes"]},
                    )
                    _pg_restore(temporary, target_pg_database, resource["table_counts"])
                    temporary.unlink()
                    continue
                if resource["kind"] == "sqlite":
                    destination = _restore_target_path(target, resource["path"])
                    _extract_member(
                        tar,
                        resource["archive_path"],
                        destination,
                        {"sha256": resource["sha256"], "size_bytes": resource["size_bytes"]},
                    )
                    counts, sensitive = _sqlite_table_counts(
                        destination, allow_exchange_verifier=_is_approved_exchange_store(resource)
                    )
                    if counts != resource["table_counts"] or any(sensitive.values()):
                        raise BackupError("restored SQLite database counts do not reconcile")
                    continue
                for file_record in resource["files"]:
                    destination = _restore_target_path(
                        target,
                        str(PurePosixPath(resource["path"], file_record["relative_path"])),
                    )
                    _extract_member(tar, file_record["archive_path"], destination, file_record)
        restore_receipt["status"] = "RESTORED"
        restore_receipt["completed_at"] = _iso(_now())
        _atomic_json(restore_receipt_path, restore_receipt)
        _fsync_directory(target)
    except BaseException as original_error:
        restore_receipt["status"] = "FAILED_RETAINED"
        restore_receipt["failed_at"] = _iso(_now())
        try:
            _atomic_json(restore_receipt_path, restore_receipt)
        except (OSError, BackupError):
            original_error.add_note(
                "The failure receipt could not be updated; inspect the private restore target."
            )
        raise
    print(
        f"restore_id={restore_receipt['restore_id']} status=RESTORED resources={len(manifest['resources'])}"
    )
    print(f"restore_target={target}")
    print(f"postgres_restore_database={target_pg_database}")
    print(
        "effects_suspended=true outbound_sends_suspended=true schedules_suspended=true replay_disabled=true"
    )
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    """Marks a fresh restore target inactive while preserving original and restored data.

    中文：将新恢复目标标记为停用，保留原始状态和恢复目标以供审计。
    """
    receipt_path = Path(args.restore_receipt).absolute()
    if receipt_path.name != "restore-receipt.json":
        raise BackupError("rollback input must be a restore receipt")
    receipt = _read_json(receipt_path)
    if receipt.get("format") != RESTORE_FORMAT or receipt.get("status") != "RESTORED":
        raise BackupError("rollback requires a completed fresh-restore receipt")
    target_root = receipt_path.parent
    if target_root.parent != STATE_DIR.parent or target_root.name != receipt.get(
        "target_root_label"
    ):
        raise BackupError("rollback target does not match its recorded private state namespace")
    _secure_private_directory(target_root, create=False)
    marker = target_root / "RESTORE_SUSPENDED.json"
    marker_value = _read_json(marker)
    if (
        marker_value.get("status") != "SUSPENDED"
        or marker_value.get("effects_enabled") is not False
    ):
        raise BackupError("restore target is not in the suspended state required for rollback")
    rollback = {
        "format": ROLLBACK_FORMAT,
        "restore_id": receipt["restore_id"],
        "snapshot_id": receipt["snapshot_id"],
        "snapshot_archive_sha256": receipt["snapshot_archive_sha256"],
        "original_state_root_label": STATE_DIR.name,
        "original_postgres_database": "cyrene_aether",
        "restore_target_label": target_root.name,
        "restore_postgres_database": receipt["target_postgres_database"],
        "status": "ROLLED_BACK_TO_PRESERVED_ORIGINAL",
        "original_state_modified": False,
        "restore_target_deleted": False,
        "historical_cutover_performed": False,
        "created_at": _iso(_now()),
    }
    _atomic_json(target_root / "ROLLBACK.json", rollback)
    receipt["status"] = "ROLLED_BACK_TO_PRESERVED_ORIGINAL"
    receipt["rolled_back_at"] = rollback["created_at"]
    _atomic_json(receipt_path, receipt)
    print(f"restore_id={receipt['restore_id']} rollback=ROLLED_BACK_TO_PRESERVED_ORIGINAL")
    print(
        f"original_state={STATE_DIR.name} original_state_modified=false restore_target_retained=true"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Full Aether task-state backup, validation, fresh restore, and safe rollback."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser(
        "backup", help="Create a complete quiesced application-state snapshot."
    )
    backup.add_argument(
        "--catalog", required=True, help="Private canonical source-catalog JSON file."
    )
    backup.add_argument(
        "--quiescence-receipt", required=True, help="Fresh owner attestation for the task stack."
    )
    backup.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Private snapshot directory outside live state.",
    )
    backup.add_argument(
        "--snapshot-id", help="Optional generated-format snapshot ID for controlled recovery."
    )
    backup.set_defaults(handler=cmd_backup)
    verify = subparsers.add_parser(
        "verify", help="Verify a completed snapshot and reconcile its resources."
    )
    verify.add_argument("--snapshot", required=True, help="Published snapshot directory.")
    verify.set_defaults(handler=cmd_verify)
    restore = subparsers.add_parser(
        "restore", help="Restore into a new private target and PostgreSQL database."
    )
    restore.add_argument("--snapshot", required=True, help="Published snapshot directory.")
    restore.add_argument(
        "--quiescence-receipt", required=True, help="Fresh owner attestation for the restore drill."
    )
    restore.add_argument(
        "--target-root",
        required=True,
        help="New sibling path named cyrene-aether-wsl-restore-<id>.",
    )
    restore.set_defaults(handler=cmd_restore)
    rollback = subparsers.add_parser(
        "rollback", help="Keep the original active and mark a fresh restore inactive."
    )
    rollback.add_argument(
        "--restore-receipt", required=True, help="Receipt created by the fresh restore command."
    )
    rollback.set_defaults(handler=cmd_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.handler(args))
    except BackupError as exc:
        print(f"aether_wsl_backup: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "aether_wsl_backup: interrupted; partial state retained for inspection", file=sys.stderr
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
