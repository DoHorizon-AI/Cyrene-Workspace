"""Exercise owner-scoped pilot role reconciliation without a real Directory.

中文：使用 mock CLI 验证 owner-scoped 试点角色同步，不连接真实 Directory。
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pilot_subjects = importlib.import_module("pilot_subjects")

TENANT = "11111111-1111-4111-8111-111111111111"
OPERATOR = "22222222-2222-4222-8222-222222222222"
PILOT = "33333333-3333-4333-8333-333333333333"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
ORGANIZATION = "org-test"
WORKSPACE = "workspace-test"
OTHER_ROLE = "workspace.research.viewer.v1"


@pytest.fixture
def actor_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a private, correctly scoped projection containing only the owner."""

    tmp_path.chmod(0o700)
    monkeypatch.setenv("CYRENE_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setenv("CYRENE_ORGANIZATION_ID", ORGANIZATION)
    monkeypatch.setenv("CYRENE_AUTHORITY_AAD_TENANT_ID", TENANT)
    monkeypatch.setenv("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID", OPERATOR)
    path = tmp_path / "teams-actor-subjects.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workspaceId": WORKSPACE,
                "issuer": ISSUER,
                "subjects": [OPERATOR],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def directory_state(
    subject: str = PILOT,
    *,
    roles: list[str] | None = None,
    is_member: bool = True,
    **scope_overrides: str,
) -> dict[str, Any]:
    """Build the exact JSON shape returned by the fixed Directory read command."""

    return {
        "issuer": scope_overrides.get("issuer", ISSUER),
        "subject": scope_overrides.get("subject", subject),
        "organization": scope_overrides.get("organization", ORGANIZATION),
        "workspace": scope_overrides.get("workspace", WORKSPACE),
        "isMember": is_member,
        "roles": list(roles or []),
    }


def read_command(command: list[str]) -> bool:
    """Identify the read-only scoped membership query, not a Directory write."""

    return command[1:3] == ["membership", "get"]


def assert_read_command(command: list[str], subject: str = PILOT) -> None:
    """Check every identity and target field passed to the Directory reader."""

    assert command == [
        pilot_subjects.CLI,
        "membership",
        "get",
        "--issuer",
        ISSUER,
        "--subject",
        subject,
        "--organization",
        ORGANIZATION,
        "--workspace",
        WORKSPACE,
    ]


def projection_subjects(path: Path) -> list[str]:
    """Return the owner-only actor projection subjects for assertions."""

    return json.loads(path.read_text(encoding="utf-8"))["subjects"]


def test_add_existing_member_grants_only_viewer_and_preserves_operator_roles(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    states = iter(
        [
            directory_state(roles=[pilot_subjects.CONNECTOR_OPERATOR_ROLE, OTHER_ROLE]),
            directory_state(
                roles=[
                    pilot_subjects.CONNECTOR_OPERATOR_ROLE,
                    OTHER_ROLE,
                    pilot_subjects.VIEWER_ROLE,
                ]
            ),
        ]
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if read_command(command):
            assert_read_command(command)
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        assert command[1] == "grant-role"
        assert command[command.index("--role") + 1] == pilot_subjects.VIEWER_ROLE
        assert command.count("--role") == 1
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    result = pilot_subjects.apply_change("add", PILOT, "approved pilot", actor_projection)

    assert result["status"] == "UPDATED"
    assert result["roles"] == sorted(
        [pilot_subjects.CONNECTOR_OPERATOR_ROLE, OTHER_ROLE, pilot_subjects.VIEWER_ROLE]
    )
    assert projection_subjects(actor_projection) == sorted([OPERATOR, PILOT])
    assert sum(read_command(command) for command in calls) == 2
    assert sum(command[1] == "grant-role" for command in calls) == 1
    assert all(command[1] != "maintainer-add" for command in calls)


def test_add_nonmember_uses_grant_membership_with_only_viewer_role(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    states = iter(
        [
            directory_state(roles=[], is_member=False),
            directory_state(roles=[pilot_subjects.VIEWER_ROLE]),
        ]
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if read_command(command):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        assert command[1] == "grant-membership"
        assert command[command.index("--role") + 1] == pilot_subjects.VIEWER_ROLE
        assert command.count("--role") == 1
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    result = pilot_subjects.apply_change("add", PILOT, "approved pilot", actor_projection)

    assert result["isMember"] is True
    assert result["roles"] == [pilot_subjects.VIEWER_ROLE]
    assert projection_subjects(actor_projection) == sorted([OPERATOR, PILOT])


def test_add_unknown_result_is_reconciled_by_readback_without_replay(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    states = iter(
        [
            directory_state(roles=[OTHER_ROLE]),
            directory_state(roles=[OTHER_ROLE, pilot_subjects.VIEWER_ROLE]),
        ]
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if read_command(command):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        raise subprocess.TimeoutExpired(command, timeout=60)

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    result = pilot_subjects.apply_change("add", PILOT, "approved pilot", actor_projection)

    assert result["status"] == "RECONCILED"
    assert len([command for command in calls if command[1] == "grant-role"]) == 1
    assert projection_subjects(actor_projection) == sorted([OPERATOR, PILOT])


def test_add_unknown_result_without_confirmation_does_not_replay_or_project(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    unchanged = directory_state(roles=[OTHER_ROLE])
    states = iter([unchanged, unchanged])

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if read_command(command):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        raise subprocess.TimeoutExpired(command, timeout=60)

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    with pytest.raises(pilot_subjects.PilotSubjectError, match="UNKNOWN"):
        pilot_subjects.apply_change("add", PILOT, "approved pilot", actor_projection)

    assert len([command for command in calls if command[1] == "grant-role"]) == 1
    assert projection_subjects(actor_projection) == [OPERATOR]


def test_add_wrong_scope_readback_fails_before_directory_write(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(directory_state(roles=[], organization="other-org")),
            stderr="",
        )

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    with pytest.raises(pilot_subjects.PilotSubjectError, match="invalid scope or schema"):
        pilot_subjects.apply_change("add", PILOT, "approved pilot", actor_projection)

    assert len(calls) == 1
    assert read_command(calls[0])
    assert projection_subjects(actor_projection) == [OPERATOR]


def test_add_unexpected_post_roles_are_not_projected(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    states = iter(
        [
            directory_state(roles=[pilot_subjects.CONNECTOR_OPERATOR_ROLE]),
            directory_state(
                roles=[
                    pilot_subjects.CONNECTOR_OPERATOR_ROLE,
                    pilot_subjects.VIEWER_ROLE,
                    OTHER_ROLE,
                ]
            ),
        ]
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if read_command(command):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    with pytest.raises(pilot_subjects.PilotSubjectError, match="not verified"):
        pilot_subjects.apply_change("add", PILOT, "approved pilot", actor_projection)

    assert projection_subjects(actor_projection) == [OPERATOR]
    assert len([command for command in calls if command[1] == "grant-role"]) == 1


def test_add_projection_failure_reports_directory_success_as_partial_state(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    states = iter(
        [
            directory_state(roles=[OTHER_ROLE]),
            directory_state(roles=[OTHER_ROLE, pilot_subjects.VIEWER_ROLE]),
        ]
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if read_command(command):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def fail_replace(*_: Any, **__: Any) -> None:
        raise OSError("synthetic projection storage failure")

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)
    monkeypatch.setattr(pilot_subjects, "_atomic_replace", fail_replace)

    with pytest.raises(pilot_subjects.PilotSubjectError, match="Directory state is verified"):
        pilot_subjects.apply_change("add", PILOT, "approved pilot", actor_projection)

    assert len([command for command in calls if command[1] == "grant-role"]) == 1
    assert projection_subjects(actor_projection) == [OPERATOR]


def test_revoke_viewer_only_removes_projection_before_mutation_and_preserves_owner(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor_projection.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workspaceId": WORKSPACE,
                "issuer": ISSUER,
                "subjects": [OPERATOR, PILOT],
            }
        ),
        encoding="utf-8",
    )
    actor_projection.chmod(0o600)
    calls: list[list[str]] = []
    states = iter(
        [
            directory_state(roles=[pilot_subjects.VIEWER_ROLE]),
            directory_state(roles=[]),
        ]
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if read_command(command):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        assert command[1] == "revoke-role"
        assert command[command.index("--role") + 1] == pilot_subjects.VIEWER_ROLE
        assert projection_subjects(actor_projection) == [OPERATOR]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    result = pilot_subjects.apply_change("revoke", PILOT, "pilot ended", actor_projection)

    assert result["isMember"] is True
    assert result["roles"] == []
    assert result["projectionAdmitted"] is False
    assert projection_subjects(actor_projection) == [OPERATOR]
    assert sum(command[1] == "revoke-role" for command in calls) == 1


def test_revoke_preserves_connector_operator_role_and_projection(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor_projection.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workspaceId": WORKSPACE,
                "issuer": ISSUER,
                "subjects": [OPERATOR, PILOT],
            }
        ),
        encoding="utf-8",
    )
    actor_projection.chmod(0o600)
    states = iter(
        [
            directory_state(
                roles=[pilot_subjects.CONNECTOR_OPERATOR_ROLE, pilot_subjects.VIEWER_ROLE]
            ),
            directory_state(roles=[pilot_subjects.CONNECTOR_OPERATOR_ROLE]),
        ]
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if read_command(command):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        assert command[1] == "revoke-role"
        assert command[command.index("--role") + 1] == pilot_subjects.VIEWER_ROLE
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    result = pilot_subjects.apply_change("revoke", PILOT, "pilot ended", actor_projection)

    assert result["roles"] == [pilot_subjects.CONNECTOR_OPERATOR_ROLE]
    assert result["projectionAdmitted"] is True
    assert projection_subjects(actor_projection) == sorted([OPERATOR, PILOT])


def test_revoke_unknown_result_stays_closed_without_replay(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor_projection.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workspaceId": WORKSPACE,
                "issuer": ISSUER,
                "subjects": [OPERATOR, PILOT],
            }
        ),
        encoding="utf-8",
    )
    actor_projection.chmod(0o600)
    calls: list[list[str]] = []
    unchanged = directory_state(roles=[pilot_subjects.VIEWER_ROLE])
    states = iter([unchanged, unchanged])

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if read_command(command):
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(next(states)), stderr=""
            )
        assert projection_subjects(actor_projection) == [OPERATOR]
        raise subprocess.TimeoutExpired(command, timeout=60)

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    with pytest.raises(pilot_subjects.PilotSubjectError, match="UNKNOWN"):
        pilot_subjects.apply_change("revoke", PILOT, "pilot ended", actor_projection)

    assert len([command for command in calls if command[1] == "revoke-role"]) == 1
    assert projection_subjects(actor_projection) == [OPERATOR]


def test_readback_reports_directory_and_projection_separately_without_writes(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(directory_state(roles=[pilot_subjects.VIEWER_ROLE])),
            stderr="",
        )

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)

    result = pilot_subjects.readback(PILOT, actor_projection)

    assert result["status"] == "READ_BACK"
    assert result["rolePresent"] is True
    assert result["projectionAdmitted"] is False
    assert len(calls) == 1
    assert_read_command(calls[0])
    assert projection_subjects(actor_projection) == [OPERATOR]


def test_owner_subject_cannot_be_changed_by_pilot_helper(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        pilot_subjects.subprocess,
        "run",
        lambda command, **kwargs: calls.append(list(command)),
    )

    with pytest.raises(pilot_subjects.PilotSubjectError, match="configured owner"):
        pilot_subjects.apply_change("revoke", OPERATOR, "pilot ended", actor_projection)

    assert calls == []
    assert projection_subjects(actor_projection) == [OPERATOR]


def test_projection_close_failure_prevents_directory_revoke(
    actor_projection: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor_projection.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "workspaceId": WORKSPACE,
                "issuer": ISSUER,
                "subjects": [OPERATOR, PILOT],
            }
        ),
        encoding="utf-8",
    )
    actor_projection.chmod(0o600)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(directory_state(roles=[pilot_subjects.VIEWER_ROLE])),
            stderr="",
        )

    def fail_replace(*_: Any, **__: Any) -> None:
        raise OSError("synthetic projection storage failure")

    monkeypatch.setattr(pilot_subjects.subprocess, "run", fake_run)
    monkeypatch.setattr(pilot_subjects, "_atomic_replace", fail_replace)

    with pytest.raises(pilot_subjects.PilotSubjectError, match="projection could not be updated"):
        pilot_subjects.apply_change("revoke", PILOT, "pilot ended", actor_projection)

    assert sum(command[1] == "revoke-role" for command in calls) == 0
    assert projection_subjects(actor_projection) == sorted([OPERATOR, PILOT])
