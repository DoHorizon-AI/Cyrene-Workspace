#!/usr/bin/env python3
"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 maintainer_subjects.py                                               │
│  Module: tooling.deploy.teams_cloud.maintainer_subjects                  │
│  Role: Apply an owner-scoped Directory change and sync Teams admission.  │
│                                                                          │
│  模块职责：先执行 owner Directory 增撤，再原子更新 Teams admission 投影。│
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SUBJECT_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ISSUER_PREFIX = "https://login.microsoftonline.com/"
CLI = "/usr/local/bin/cy-workspace-directory-admin"
DEFAULT_FILE = "/run/cyrene-config/teams-actor-subjects.json"


class MaintainerError(ValueError):
    """Directory identity or projection update could not be proven complete."""


def _scope() -> tuple[str, str, str, str]:
    """Read the fixed owner/workspace scope from non-secret environment values."""

    workspace = os.environ.get("CYRENE_WORKSPACE_ID", "")
    organization = os.environ.get("CYRENE_ORGANIZATION_ID", "")
    tenant = os.environ.get("CYRENE_AUTHORITY_AAD_TENANT_ID", "")
    operator = os.environ.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID", "")
    if not all((workspace, organization, tenant, operator)):
        raise MaintainerError("owner, tenant, organization, and workspace scope are required")
    if not SUBJECT_RE.fullmatch(operator):
        raise MaintainerError("configured Directory operator identity is not an AAD object UUID")
    if operator != operator.lower():
        raise MaintainerError("configured Directory operator identity must be lowercase")
    if not SUBJECT_RE.fullmatch(tenant):
        raise MaintainerError("configured tenant ID is malformed")
    if tenant != tenant.lower():
        raise MaintainerError("configured tenant ID must be lowercase")
    if any(
        not value or value.strip() != value or len(value) > 256
        for value in (workspace, organization)
    ):
        raise MaintainerError("configured organization or workspace ID is malformed")
    return workspace, organization, tenant, operator


def _read_projection(
    path: Path, *, workspace: str, issuer: str, allow_missing: bool = False
) -> dict[str, Any]:
    """Require a private, correctly scoped actor projection before changing it."""

    if path.name != "teams-actor-subjects.json" or path.is_symlink():
        raise MaintainerError("Teams actor-subject projection is unsafe")
    parent = path.parent
    if (
        parent.is_symlink()
        or not parent.is_dir()
        or parent.stat().st_uid != os.geteuid()
        or parent.stat().st_mode & 0o077
    ):
        raise MaintainerError("private configuration directory has unsafe permissions")
    if not path.exists() and allow_missing:
        return {"schemaVersion": 1, "workspaceId": workspace, "issuer": issuer, "subjects": []}
    if not path.is_file():
        raise MaintainerError("Teams actor-subject projection is unavailable")
    metadata = path.stat()
    if metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid():
        raise MaintainerError(
            "Teams actor-subject projection must be owner-only and owned by this operator"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MaintainerError("Teams actor-subject projection is invalid JSON") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "workspaceId", "issuer", "subjects"}
        or document.get("schemaVersion") != 1
        or document.get("workspaceId") != workspace
        or document.get("issuer") != issuer
        or not isinstance(document.get("subjects"), list)
        or any(
            not isinstance(subject, str)
            or not SUBJECT_RE.fullmatch(subject)
            or subject != subject.lower()
            for subject in document["subjects"]
        )
        or len(set(document["subjects"])) != len(document["subjects"])
    ):
        raise MaintainerError(
            "Teams actor-subject projection scope or schema does not match this deployment"
        )
    return document


def _atomic_replace(path: Path, document: dict[str, Any]) -> None:
    """Write a restrictive replacement in the same mounted directory, then fsync."""

    data = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, filename = tempfile.mkstemp(
        prefix=".teams-actor-subjects.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(filename)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:  # noqa: BLE001 - remove the temporary projection on cancellation too.
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


@contextlib.contextmanager
def _projection_lock(path: Path):
    """Serialize one-shot Directory changes with their actor-file replacement."""

    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise MaintainerError("Teams actor projection lock has unsafe owner or permissions")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintainerError("another Directory maintainer update is in progress") from error
        yield
    finally:
        os.close(descriptor)


def apply_change(action: str, subject: str, reason: str, path: Path) -> dict[str, Any]:
    """Run Directory first; update the admission projection only after success."""

    if (
        action not in {"add", "revoke"}
        or not isinstance(subject, str)
        or not SUBJECT_RE.fullmatch(subject)
    ):
        raise MaintainerError("action or target subject is malformed")
    subject = subject.lower()
    if (
        action == "revoke"
        and subject.lower() == os.environ.get("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID", "").lower()
    ):
        raise MaintainerError("the owner operator cannot revoke its own provisioning identity")
    if not reason.strip() or len(reason) > 240:
        raise MaintainerError("an audit reason of 1 to 240 characters is required")
    workspace, organization, tenant, operator = _scope()
    issuer = ISSUER_PREFIX + tenant + "/v2.0"
    with _projection_lock(path):
        projection = _read_projection(
            path,
            workspace=workspace,
            issuer=issuer,
            allow_missing=action == "add" and subject == operator.lower(),
        )
        command = [
            CLI,
            "maintainer",
            action,
            "--issuer",
            issuer,
            "--subject",
            subject,
            "--organization",
            organization,
            "--workspace",
            workspace,
            "--reason",
            reason,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except subprocess.TimeoutExpired as error:
            raise MaintainerError(
                "Directory operation outcome is UNKNOWN; read Directory state before retrying"
            ) from error
        except OSError as error:
            raise MaintainerError("Directory maintainer command could not be started") from error
        if result.returncode != 0:
            raise MaintainerError(
                "Directory rejected the maintainer change; the actor projection was not changed"
            )

        subjects = list(projection["subjects"])
        if action == "add" and subject not in subjects:
            subjects.append(subject)
        elif action == "revoke":
            subjects = [current for current in subjects if current != subject]
        projection["subjects"] = sorted(subjects)
        try:
            _atomic_replace(path, projection)
        except OSError as error:
            raise MaintainerError(
                "Directory change succeeded but the Teams actor projection could not be updated; repair the file before retrying"
            ) from error
    return {"status": "UPDATED", "action": action, "workspaceId": workspace, "issuer": issuer}


def main() -> int:
    """Run one host-invoked, owner-scoped Directory membership change."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("add", "revoke"))
    parser.add_argument("--subject", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--projection-file", type=Path, default=Path(DEFAULT_FILE))
    arguments = parser.parse_args()
    try:
        result = apply_change(
            arguments.action, arguments.subject, arguments.reason, arguments.projection_file
        )
    except MaintainerError as error:
        print(f"Directory maintainer update failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
