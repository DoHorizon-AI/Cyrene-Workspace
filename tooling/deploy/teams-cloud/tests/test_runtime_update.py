"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_runtime_update.py                                               │
│  Module: tooling.deploy.teams_cloud.tests.test_runtime_update            │
│  Role: Verify bounded runtime-update safety and journal contracts.       │
│                                                                          │
│  模块职责：验证热更新的边界、失败恢复与持久操作回执。                    │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
runtime_update = importlib.import_module("runtime_update")

OLD_IMAGE_ID = "sha256:" + "a" * 64
NEW_IMAGE_ID = "sha256:" + "b" * 64
OLD_CONTAINER_ID = "1" * 64
NEW_CONTAINER_ID = "2" * 64
OPERATION_ID = "00000000-0000-4000-8000-000000000001"


def _edge_status(*, paused: bool, revision: int, active: int = 0) -> dict[str, Any]:
    """Return the exact safe status envelope used by the Teams delivery admin API."""

    return {
        "paused": paused,
        "revision": revision,
        "active_delivery_count": active,
        "queued_count": 0,
        "leased_count": 0,
        "retry_wait_count": 0,
        "delivered_count": 3,
        "rejected_count": 0,
        "dead_count": 0,
        "pending_bytes": 0,
        "retained_body_bytes": 0,
        "oldest_pending_at": None,
        "last_delivered_at": None,
    }


def _active_service(container_id: str, image_id: str, image_ref: str) -> dict[str, Any]:
    """Build one sanitized exact container snapshot."""

    return {
        "containerId": container_id,
        "imageRef": image_ref,
        "imageId": image_id,
        "health": "healthy",
        "networkNames": ["test_private"],
    }


def test_runtime_plan_maps_compose_navigator_to_the_canonical_dsh_lock_row() -> None:
    """Bind the Compose navigator service to the build system's DSH executor identity."""

    compose = {"services": {"navigator": {"image": "dsh:navigator-v1"}}}
    lock = {
        "images": [
            {
                "service": "dsh-executor",
                "image": "dsh:navigator-v1",
                "imageId": OLD_IMAGE_ID,
                "sourceCommits": {"navigator": "a" * 40},
            }
        ]
    }

    assert runtime_update._target_images(compose, lock, ["navigator"]) == {
        "navigator": {
            "lockService": "dsh-executor",
            "image": "dsh:navigator-v1",
            "imageId": OLD_IMAGE_ID,
            "sourceCommits": {"navigator": "a" * 40},
        }
    }
    with pytest.raises(runtime_update.RuntimeUpdateError, match="canonical dsh-executor"):
        runtime_update._target_images(
            compose,
            {"images": [{**lock["images"][0], "service": "navigator"}]},
            ["navigator"],
        )


def test_plan_cli_parses_the_noninteractive_docker_prefix_as_one_closed_choice() -> None:
    """Keep the documented sudo prefix parseable without shell or argv ambiguity."""

    arguments = runtime_update._build_parser().parse_args(
        [
            "plan",
            "--active-compose-file",
            "/private/active.yaml",
            "--active-env-file",
            "/private/runtime.env",
            "--active-image-lock",
            "/private/active-lock.json",
            "--candidate-compose-file",
            "/private/candidate.yaml",
            "--candidate-env-file",
            "/private/candidate.env",
            "--candidate-image-lock",
            "/private/candidate-lock.json",
            "--secrets-dir",
            "/private/secrets",
            "--config-dir",
            "/private/config",
            "--docker-prefix",
            "sudo-n-docker",
            "--target",
            "navigator",
            "--output",
            "/private/.artifacts/plan.json",
        ]
    )

    assert arguments.docker_prefix == "sudo-n-docker"
    assert arguments.target == ["navigator"]


def test_candidate_lock_cannot_change_any_non_target_image_row() -> None:
    """Permit a selected target update while keeping all other locked images exact."""

    active = {
        "images": [
            {"service": "dsh-executor", "image": "nav:old", "imageId": OLD_IMAGE_ID},
            {"service": "exchange", "image": "exchange:v1", "imageId": OLD_IMAGE_ID},
        ],
        "externalImages": {},
    }
    candidate = {
        "images": [
            {"service": "dsh-executor", "image": "nav:new", "imageId": NEW_IMAGE_ID},
            {"service": "exchange", "image": "exchange:v1", "imageId": OLD_IMAGE_ID},
        ],
        "externalImages": {},
    }

    runtime_update._validate_lock_pair(active, candidate, ["navigator"])

    candidate["images"][1]["imageId"] = NEW_IMAGE_ID
    with pytest.raises(
        runtime_update.RuntimeUpdateError, match="non-target image service exchange"
    ):
        runtime_update._validate_lock_pair(active, candidate, ["navigator"])


def test_navigator_exit_137_is_never_reported_as_a_graceful_stop() -> None:
    """Require the real container exit code to confirm clean executor teardown."""

    stopped = {
        "Status": "exited",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "OOMKilled": False,
        "ExitCode": 0,
        "Error": "",
        "StartedAt": "2026-10-25T18:29:00Z",
        "FinishedAt": "2026-10-25T18:30:00Z",
    }

    assert runtime_update._navigator_stop_is_graceful(0, stopped)
    assert not runtime_update._navigator_stop_is_graceful(0, {**stopped, "ExitCode": 137})
    assert not runtime_update._navigator_stop_is_graceful(0, {**stopped, "OOMKilled": True})
    assert not runtime_update._navigator_stop_is_graceful(None, stopped)


def test_update_plan_requires_compose_stop_grace_to_cover_executor_teardown() -> None:
    """Reject altered shutdown budgets that could make Docker force-kill Navigator."""

    service = {
        "environment": {
            "CYRENE_EXECUTOR_DRAIN_TIMEOUT_MS": "30000",
            "CYRENE_EXECUTOR_ABORT_CLEANUP_TIMEOUT_MS": "5000",
        },
        "stop_grace_period": "40s",
    }

    runtime_update._validate_navigator_stop_contract(service)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="stop budget"):
        runtime_update._validate_navigator_stop_contract(
            {
                **service,
                "environment": {
                    **service["environment"],
                    "CYRENE_EXECUTOR_DRAIN_TIMEOUT_MS": "60000",
                },
            }
        )
    with pytest.raises(runtime_update.RuntimeUpdateError, match="stop budget"):
        runtime_update._validate_navigator_stop_contract({**service, "stop_grace_period": "10s"})


def test_internal_status_validation_preserves_the_json_document() -> None:
    """Keep sanitized API status fields available after removing the HTTP envelope."""

    response = {
        "_httpStatus": 200,
        "mode": "accepting",
        "generation": 0,
        "token": None,
        "startedAt": None,
        "instanceId": "instance-1",
        "activeTasks": 0,
        "queuedTasks": 2,
        "notifications": {"pollingPaused": False, "inFlight": False},
        "activeWorkDrained": False,
    }

    runtime_update._validate_navigator_status(response)

    assert response["mode"] == "accepting"
    assert response["queuedTasks"] == 2
    assert "_httpStatus" not in response


def test_lost_edge_resume_reply_reacquires_the_pause_with_a_fresh_cas_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Re-pause delivery before rollback when a prior resume may have taken effect."""

    journal = {
        "operationId": OPERATION_ID,
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialRevision": 0,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "pauseRevision": 1,
            "resumeRevision": 2,
            "pauseHistory": [1],
            "repauseExpectedRevision": None,
        },
        "phase": "EDGE_RESUME_REQUESTED",
        "state": "RUNNING",
        "history": [],
    }
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args: _edge_status(paused=False, revision=2),
    )
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)

    def call_internal_api(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["body"])
        return {"_httpStatus": 200, **_edge_status(paused=True, revision=3)}

    monkeypatch.setattr(runtime_update, "_call_internal_api", call_internal_api)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)

    runtime_update._recover_edge_pause(
        journal=journal,
        journal_path=tmp_path / "operation.json",
        docker_prefix=["docker"],
        timeout_seconds=60,
    )

    assert calls == [{"holder": OPERATION_ID, "expected_revision": 2}]
    assert journal["edge"]["pauseRevision"] == 3
    assert journal["edge"]["resumeRevision"] is None
    assert journal["edge"]["pauseHistory"] == [1, 3]


def test_failed_candidate_navigator_drain_blocks_compose_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep the edge held and never replace a candidate whose active work is unknown."""

    plan = {
        "project": "test-project",
        "targets": ["navigator"],
        "candidateImages": {
            "navigator": {
                "lockService": "dsh-executor",
                "image": "nav:new",
                "imageId": NEW_IMAGE_ID,
                "sourceCommits": {},
            }
        },
        "activeServices": {"navigator": _active_service(OLD_CONTAINER_ID, OLD_IMAGE_ID, "nav:old")},
        "policy": {"drainTimeoutSeconds": 60},
    }
    journal = {
        "operationId": OPERATION_ID,
        "phase": "NAVIGATOR_HEALTHY",
        "state": "RUNNING",
        "targets": ["navigator"],
        "updatedTargets": ["navigator"],
        "targetProgress": {
            "navigator": {
                "phase": "UPDATED_HEALTHY",
                "oldContainerId": OLD_CONTAINER_ID,
                "oldImageId": OLD_IMAGE_ID,
                "candidateImageId": NEW_IMAGE_ID,
            }
        },
        "edge": {
            "containerId": "3" * 64,
            "initialRevision": 0,
            "initialDeadCount": 0,
            "pauseRevision": 1,
        },
        "navigator": {
            "initialContainerId": OLD_CONTAINER_ID,
            "initialInstanceId": "old-instance",
            "initialGeneration": 0,
            "drainGeneration": 1,
            "drained": True,
        },
    }
    candidate = _active_service(NEW_CONTAINER_ID, NEW_IMAGE_ID, "nav:new")
    current_state = {"navigator": candidate}
    events: list[str] = []

    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime_update,
        "_recover_edge_pause",
        lambda **_kwargs: events.append("edge-held"),
    )
    monkeypatch.setattr(runtime_update, "_read_transition_state", lambda **_kwargs: current_state)

    def drain_candidate(**_kwargs: Any) -> None:
        events.append("candidate-drain-unverified")
        raise runtime_update.RuntimeUpdateError("candidate executor API unavailable")

    monkeypatch.setattr(runtime_update, "_drain_current_navigator_for_rollback", drain_candidate)
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **_kwargs: pytest.fail("Compose rollback must wait for candidate work drain proof"),
    )

    with pytest.raises(runtime_update.RuntimeUpdateError, match="API unavailable"):
        runtime_update._rollback_operation(
            plan=plan,
            context={"activeCompose": {}, "activeLock": {}},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )

    assert events == ["edge-held", "candidate-drain-unverified"]


def test_resume_blocks_when_a_candidate_navigator_container_may_have_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Do not start the old image when any candidate container may have claimed queued work."""

    plan = {
        "project": "test-project",
        "targets": ["navigator"],
        "activeServices": {"navigator": _active_service(OLD_CONTAINER_ID, OLD_IMAGE_ID, "nav:old")},
        "candidateImages": {
            "navigator": {
                "lockService": "dsh-executor",
                "image": "nav:new",
                "imageId": NEW_IMAGE_ID,
                "sourceCommits": {},
            }
        },
    }
    journal = {
        "phase": "ROLLBACK_STARTING",
        "targets": ["navigator"],
        "updatedTargets": [],
        "targetProgress": {"navigator": {"phase": "PREPARED"}},
        "navigator": {
            "stop": {"containerId": OLD_CONTAINER_ID},
            "processRestart": None,
        },
    }
    monkeypatch.setattr(
        runtime_update,
        "_list_project_navigator_containers",
        lambda **_kwargs: [
            {
                "containerId": NEW_CONTAINER_ID,
                "imageRef": "nav:new",
                "imageId": NEW_IMAGE_ID,
                "running": False,
            }
        ],
    )
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **_kwargs: pytest.fail("the previous image must not replace an undrained candidate"),
    )

    with pytest.raises(runtime_update.RuntimeUpdateError, match="cannot be proven drained"):
        runtime_update._recover_old_navigator_before_candidate_start(
            plan=plan,
            context={},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )


def test_resume_restores_the_drained_old_image_before_candidate_compose_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recover an interrupted old-container stop only while the journal proves no candidate ran."""

    plan = {
        "project": "test-project",
        "targets": ["navigator"],
        "activeInputs": {"composeFiles": [], "envFiles": []},
        "activeServices": {"navigator": _active_service(OLD_CONTAINER_ID, OLD_IMAGE_ID, "nav:old")},
        "candidateImages": {
            "navigator": {
                "lockService": "dsh-executor",
                "image": "nav:new",
                "imageId": NEW_IMAGE_ID,
                "sourceCommits": {},
            }
        },
        "policy": {"healthTimeoutSeconds": 30},
    }
    journal = {
        "operationId": OPERATION_ID,
        "phase": "NAVIGATOR_STOP_REQUESTED",
        "state": "ROLLING_BACK",
        "targets": ["navigator"],
        "updatedTargets": [],
        "targetProgress": {
            "navigator": {
                "phase": "PREPARED",
                "oldContainerId": OLD_CONTAINER_ID,
                "oldImageId": OLD_IMAGE_ID,
            }
        },
        "navigator": {
            "initialContainerId": OLD_CONTAINER_ID,
            "initialInstanceId": "old-instance",
            "stop": {"containerId": OLD_CONTAINER_ID, "exitCode": 0},
            "processRestart": None,
        },
    }
    restored = _active_service(NEW_CONTAINER_ID, OLD_IMAGE_ID, "nav:old")
    monkeypatch.setattr(runtime_update, "_list_project_navigator_containers", lambda **_kwargs: [])
    monkeypatch.setattr(
        runtime_update, "_input_paths", lambda *_args: [Path("active-compose.yaml")]
    )
    monkeypatch.setattr(
        runtime_update, "_compose_up", lambda **_kwargs: type("Result", (), {"returncode": 0})()
    )
    monkeypatch.setattr(
        runtime_update, "_read_transition_state", lambda **_kwargs: {"navigator": restored}
    )
    monkeypatch.setattr(
        runtime_update,
        "_wait_for_exact_service_health",
        lambda **_kwargs: restored,
    )
    monkeypatch.setattr(runtime_update, "_verify_runtime_unmanaged", lambda *_args: None)
    monkeypatch.setattr(runtime_update, "_verify_live_network_membership", lambda *_args: None)
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            "mode": "accepting",
            "generation": 0,
            "token": None,
            "startedAt": None,
            "instanceId": "fresh-instance",
        },
    )

    recovered = runtime_update._recover_old_navigator_before_candidate_start(
        plan=plan,
        context={"activeCompose": {}, "activeLock": {}},
        journal=journal,
        journal_path=tmp_path / "operation.json",
        docker_prefix=["docker"],
    )

    assert recovered is True
    assert journal["targetProgress"]["navigator"]["phase"] == "RECOVERY_OLD_IMAGE_RESTARTED"
    assert journal["navigator"]["processRestart"]["activeImageVerified"] is True


def test_successful_update_releases_edge_only_after_new_navigator_is_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Preserve the sequence: durable edge hold, Navigator drain, updates, Nav ready, release."""

    plan = {"policy": {"updateOrder": ["exchange", "navigator"]}}
    journal = {
        "schemaVersion": runtime_update.JOURNAL_SCHEMA,
        "operationId": OPERATION_ID,
        "planSha256": "c" * 64,
        "state": "RUNNING",
        "phase": "PRECHECKED",
        "targets": ["exchange", "navigator"],
        "updatedTargets": [],
        "createdAtUtc": "2026-10-25T18:00:00+00:00",
        "updatedAtUtc": "2026-10-25T18:00:00+00:00",
        "lastErrorCode": None,
        "edge": {"latestDeadCount": 0},
        "navigator": {"stop": None},
    }
    events: list[str] = []
    monkeypatch.setattr(
        runtime_update, "_pause_edge", lambda **_kwargs: events.append("edge-pause")
    )
    monkeypatch.setattr(
        runtime_update, "_drain_navigator", lambda **_kwargs: events.append("nav-drain")
    )
    monkeypatch.setattr(
        runtime_update,
        "_update_target",
        lambda **kwargs: events.append(f"update-{kwargs['target']}"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_ensure_navigator_resumed",
        lambda **_kwargs: events.append("nav-ready"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_resume_edge",
        lambda **_kwargs: events.append("edge-resume"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_set_phase",
        lambda target_journal, _path, phase, *, state=None, error_code=None: target_journal.update(
            {
                "phase": phase,
                "updatedAtUtc": "2026-10-25T18:01:00+00:00",
                "state": state or target_journal["state"],
                "lastErrorCode": error_code,
            }
        ),
    )

    result = runtime_update._execute_update(
        plan=plan,
        context={},
        journal=journal,
        journal_path=tmp_path / "operation.json",
        docker_prefix=["docker"],
    )

    assert events == [
        "edge-pause",
        "nav-drain",
        "update-exchange",
        "update-navigator",
        "nav-ready",
        "edge-resume",
    ]
    assert result["state"] == "APPLIED"
    assert result["phase"] == "UPDATE_COMPLETE"
