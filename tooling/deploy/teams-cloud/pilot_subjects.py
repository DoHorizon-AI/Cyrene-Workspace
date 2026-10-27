#!/usr/bin/env python3
"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 pilot_subjects.py                                                    │
│  Module: tooling.deploy.teams_cloud.pilot_subjects                       │
│  Role: Reconcile one read-only automation pilot role with Teams admission.│
│                                                                          │
│  模块职责：按 Directory 权威角色同步单个只读试点成员与 Teams admission。│
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maintainer_subjects import (
    CLI,
    DEFAULT_FILE,
    ISSUER_PREFIX,
    SUBJECT_RE,
    MaintainerError,
    _atomic_replace,
    _projection_lock,
    _read_projection,
    _scope,
)

VIEWER_ROLE = "navigator.automation.viewer.v1"
CONNECTOR_OPERATOR_ROLE = "navigator.connector.operator.v1"
_ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_ROLE_READ_BYTES = 8_192


class PilotSubjectError(MaintainerError):
    """Directory role state or its Teams admission projection is unverified."""


@dataclass(frozen=True, slots=True)
class DirectoryRoleState:
    """One exact, read-only Directory membership and explicit-role result."""

    issuer: str
    subject: str
    organization: str
    workspace: str
    is_member: bool
    roles: frozenset[str]


def _validate_subject(subject: str) -> str:
    """Require one canonical AAD object UUID before using it in a fixed CLI call."""

    if not isinstance(subject, str) or not SUBJECT_RE.fullmatch(subject):
        raise PilotSubjectError("target subject must be an AAD object UUID")
    return subject.lower()


def _directory_scope(subject: str, *, workspace: str, organization: str, issuer: str) -> list[str]:
    """Build the closed read command for the exact configured Directory scope."""

    return [
        CLI,
        "membership",
        "get",
        "--issuer",
        issuer,
        "--subject",
        subject,
        "--organization",
        organization,
        "--workspace",
        workspace,
    ]


def _read_directory_state(
    subject: str, *, workspace: str, organization: str, issuer: str
) -> DirectoryRoleState:
    """Read and validate the exact current Directory membership and role set."""

    try:
        result = subprocess.run(
            _directory_scope(
                subject, workspace=workspace, organization=organization, issuer=issuer
            ),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise PilotSubjectError("authoritative Directory role read timed out") from error
    except OSError as error:
        raise PilotSubjectError("authoritative Directory role read could not be started") from error
    if result.returncode != 0:
        raise PilotSubjectError("authoritative Directory role read failed")
    if (
        not isinstance(result.stdout, str)
        or len(result.stdout.encode("utf-8")) > _MAX_ROLE_READ_BYTES
    ):
        raise PilotSubjectError("authoritative Directory role response is oversized")
    try:
        document = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PilotSubjectError("authoritative Directory role response is invalid JSON") from error
    expected_keys = {"issuer", "subject", "organization", "workspace", "isMember", "roles"}
    roles = document.get("roles") if isinstance(document, dict) else None
    is_member = document.get("isMember") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != expected_keys
        or document.get("issuer") != issuer
        or document.get("subject") != subject
        or document.get("organization") != organization
        or document.get("workspace") != workspace
        or not isinstance(is_member, bool)
        or not isinstance(roles, list)
        or any(not isinstance(role, str) or not _ROLE_RE.fullmatch(role) for role in roles)
        or len(set(roles)) != len(roles)
        or (not is_member and roles)
    ):
        raise PilotSubjectError("authoritative Directory role response has invalid scope or schema")
    return DirectoryRoleState(
        issuer=issuer,
        subject=subject,
        organization=organization,
        workspace=workspace,
        is_member=is_member,
        roles=frozenset(roles),
    )


def _mutation_command(
    action: str,
    subject: str,
    reason: str,
    *,
    workspace: str,
    organization: str,
    issuer: str,
    existing_member: bool,
) -> list[str]:
    """Grant only the viewer role or revoke only that role."""

    if action == "add":
        command = [CLI, "grant-role" if existing_member else "grant-membership"]
    else:
        command = [CLI, "revoke-role"]
    command.extend(
        [
            "--issuer",
            issuer,
            "--subject",
            subject,
            "--organization",
            organization,
            "--workspace",
            workspace,
            "--role",
            VIEWER_ROLE,
            "--reason",
            reason,
        ]
    )
    return command


def _run_mutation(command: list[str]) -> tuple[bool, bool]:
    """Run once; return success and timeout state without ever replaying a write."""

    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, True
    except OSError as error:
        raise PilotSubjectError("Directory role command could not be started") from error
    return result.returncode == 0, False


def _replace_projection(path: Path, projection: dict[str, Any], subjects: set[str]) -> None:
    """Atomically persist one sorted subject projection after scope validation."""

    projection["subjects"] = sorted(subjects)
    try:
        _atomic_replace(path, projection)
    except OSError as error:
        raise PilotSubjectError(
            "Directory state is verified but the Teams actor projection could not be updated"
        ) from error


def _admitted_after_revoke(subject: str, operator: str, roles: frozenset[str]) -> bool:
    """Keep configured owner and connector operators admitted after viewer removal."""

    return subject == operator or CONNECTOR_OPERATOR_ROLE in roles


def _result(
    status: str,
    state: DirectoryRoleState,
    *,
    projection_admitted: bool,
) -> dict[str, Any]:
    """Return bounded operator readback without vendor errors or credentials."""

    return {
        "status": status,
        "workspaceId": state.workspace,
        "issuer": state.issuer,
        "role": VIEWER_ROLE,
        "isMember": state.is_member,
        "rolePresent": VIEWER_ROLE in state.roles,
        "roles": sorted(state.roles),
        "projectionAdmitted": projection_admitted,
    }


def readback(subject: str, path: Path) -> dict[str, Any]:
    """Read Directory and actor projection state without changing either."""

    subject = _validate_subject(subject)
    workspace, organization, tenant, _operator = _scope()
    issuer = ISSUER_PREFIX + tenant + "/v2.0"
    with _projection_lock(path):
        projection = _read_projection(path, workspace=workspace, issuer=issuer)
        state = _read_directory_state(
            subject, workspace=workspace, organization=organization, issuer=issuer
        )
    return _result("READ_BACK", state, projection_admitted=subject in projection["subjects"])


def apply_change(action: str, subject: str, reason: str, path: Path) -> dict[str, Any]:
    """Apply one viewer-role change and reconcile its Teams admission projection."""

    if action not in {"add", "revoke"}:
        raise PilotSubjectError("action must be add or revoke")
    subject = _validate_subject(subject)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 240:
        raise PilotSubjectError("an audit reason of 1 to 240 characters is required")
    workspace, organization, tenant, operator = _scope()
    operator = operator.lower()
    if subject == operator:
        raise PilotSubjectError("pilot changes cannot alter the configured owner subject")
    issuer = ISSUER_PREFIX + tenant + "/v2.0"

    with _projection_lock(path):
        projection = _read_projection(path, workspace=workspace, issuer=issuer)
        subjects = set(projection["subjects"])
        before = _read_directory_state(
            subject, workspace=workspace, organization=organization, issuer=issuer
        )

        if action == "add":
            if VIEWER_ROLE not in before.roles:
                command = _mutation_command(
                    action,
                    subject,
                    reason,
                    workspace=workspace,
                    organization=organization,
                    issuer=issuer,
                    existing_member=before.is_member,
                )
                command_succeeded, timed_out = _run_mutation(command)
                try:
                    after = _read_directory_state(
                        subject, workspace=workspace, organization=organization, issuer=issuer
                    )
                except PilotSubjectError as error:
                    detail = (
                        "UNKNOWN; authoritative readback is unavailable"
                        if timed_out
                        else "unverified"
                    )
                    raise PilotSubjectError(
                        f"Directory role grant outcome is {detail}; do not retry before reconciliation"
                    ) from error
                expected_roles = before.roles | {VIEWER_ROLE} if before.is_member else {VIEWER_ROLE}
                if not after.is_member or after.roles != expected_roles:
                    detail = "UNKNOWN" if timed_out else "not verified"
                    if not command_succeeded and not timed_out and after == before:
                        detail = "not applied"
                    raise PilotSubjectError(
                        f"Directory role grant outcome is {detail}; projection was not changed"
                    )
                status = "UPDATED" if command_succeeded else "RECONCILED"
            else:
                after = before
                status = "ALREADY_PRESENT"

            if subject not in subjects:
                subjects.add(subject)
                _replace_projection(path, projection, subjects)
                if status == "ALREADY_PRESENT":
                    status = "PROJECTION_UPDATED"
            return _result(status, after, projection_admitted=subject in subjects)

        desired_admission = _admitted_after_revoke(subject, operator, before.roles)
        if desired_admission:
            subjects.add(subject)
        else:
            subjects.discard(subject)
        if subjects != set(projection["subjects"]):
            # Close viewer admission before revoking Directory access; a failed or
            # unknown CLI result therefore cannot leave a viewer-only subject admitted.
            _replace_projection(path, projection, subjects)

        if VIEWER_ROLE not in before.roles:
            return _result("ALREADY_ABSENT", before, projection_admitted=subject in subjects)

        command = _mutation_command(
            action,
            subject,
            reason,
            workspace=workspace,
            organization=organization,
            issuer=issuer,
            existing_member=True,
        )
        command_succeeded, timed_out = _run_mutation(command)
        try:
            after = _read_directory_state(
                subject, workspace=workspace, organization=organization, issuer=issuer
            )
        except PilotSubjectError as error:
            detail = "UNKNOWN; authoritative readback is unavailable" if timed_out else "unverified"
            raise PilotSubjectError(
                f"Directory role revoke outcome is {detail}; projection remains fail-closed"
            ) from error

        if after.is_member and VIEWER_ROLE in after.roles:
            detail = "UNKNOWN" if timed_out else "not applied"
            raise PilotSubjectError(
                f"Directory role revoke outcome is {detail}; projection remains fail-closed"
            )
        expected_roles = before.roles - {VIEWER_ROLE}
        if after.is_member != before.is_member or after.roles != expected_roles:
            # Preserve an owner/operator admission from authoritative state even when
            # an unrelated concurrent Directory change prevents a clean success.
            subjects = set(projection["subjects"])
            if _admitted_after_revoke(subject, operator, after.roles):
                subjects.add(subject)
            else:
                subjects.discard(subject)
            if subjects != set(projection["subjects"]):
                _replace_projection(path, projection, subjects)
            raise PilotSubjectError(
                "Directory roles changed concurrently; projection follows authoritative state"
            )

        if command_succeeded:
            status = "UPDATED"
        elif timed_out:
            status = "RECONCILED"
        else:
            status = "RECONCILED"
        return _result(status, after, projection_admitted=subject in subjects)


def main() -> int:
    """Run one owner-scoped pilot membership operation or readback."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("add", "revoke", "readback"))
    parser.add_argument("--subject", required=True)
    parser.add_argument("--reason")
    parser.add_argument("--projection-file", type=Path, default=Path(DEFAULT_FILE))
    arguments = parser.parse_args()
    try:
        if arguments.action == "readback":
            result = readback(arguments.subject, arguments.projection_file)
        else:
            result = apply_change(
                arguments.action,
                arguments.subject,
                arguments.reason or "",
                arguments.projection_file,
            )
    except (MaintainerError, PilotSubjectError) as error:
        print(f"Pilot Directory update failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
