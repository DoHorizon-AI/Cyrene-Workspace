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

import copy
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
runtime_update = importlib.import_module("runtime_update")

OLD_IMAGE_ID = "sha256:" + "a" * 64
NEW_IMAGE_ID = "sha256:" + "b" * 64
OLD_CONTAINER_ID = "1" * 64
NEW_CONTAINER_ID = "2" * 64
OPERATION_ID = "00000000-0000-4000-8000-000000000001"
NAVIGATOR_DRAIN_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/navigator-drain-status-18baefba.json"
)
PLUGINS_DELIVERY_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/plugins-delivery-status-cffd6786.json"
)


def _native_navigator_status(name: str) -> dict[str, Any]:
    """Load a sanitized status shape copied from the pinned Navigator producer contract."""

    fixture = json.loads(NAVIGATOR_DRAIN_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["producerCommit"] == "18baefba4f8e0c89b2ba335ea6f7d41251f1a85d"
    return copy.deepcopy(fixture["statuses"][name])


def _plugins_delivery_status() -> dict[str, Any]:
    """Load the non-empty queue/timestamp shape returned by the pinned Plugins producer."""

    fixture = json.loads(PLUGINS_DELIVERY_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["producerCommit"] == "cffd6786fbbef74dcc0797fe33e760dbda5250a8"
    return copy.deepcopy(fixture["status"])


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


def _internal_probe_container(
    *,
    container_id: str,
    service: str,
    token_path: str,
    token_source: Path,
) -> dict[str, Any]:
    """Build Docker inspect metadata for one locked service and read-only token bind."""

    return {
        "Id": container_id,
        "Image": OLD_IMAGE_ID,
        "Config": {
            "Image": f"cyrene/{service}:locked-test",
            "Labels": {
                "com.docker.compose.project": "cyrene-teams-cloud",
                "com.docker.compose.service": service,
            },
        },
        "State": {"Running": True, "Paused": False},
        "HostConfig": {"NetworkMode": "cyrene-teams-cloud_private-network"},
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(token_source.resolve()),
                "Destination": token_path,
                "RW": False,
            }
        ],
    }


def _internal_probe_inspect_output(metadata: dict[str, Any]) -> str:
    """Serialize only the target identity/network/mount fields read by the probe gate."""

    config = metadata["Config"]
    state = metadata["State"]
    host_config = metadata["HostConfig"]
    return "\n".join(
        json.dumps(value)
        for value in (
            metadata["Id"],
            metadata["Image"],
            config["Image"],
            config["Labels"],
            state["Running"],
            state["Paused"],
            host_config["NetworkMode"],
            metadata["Mounts"],
        )
    )


def _active_service(container_id: str, image_id: str, image_ref: str) -> dict[str, Any]:
    """Build one sanitized exact container snapshot."""

    return {
        "containerId": container_id,
        "imageRef": image_ref,
        "imageId": image_id,
        "health": "healthy",
        "networkNames": ["test_private"],
    }


def _reviewed_plan(
    targets: list[str], *, targets_only: list[str], update_order: list[str]
) -> dict[str, Any]:
    """Build a digest-valid sanitized plan for target-policy compatibility checks."""

    navigator = _active_service(OLD_CONTAINER_ID, OLD_IMAGE_ID, "navigator:active")
    ingress = _active_service(NEW_CONTAINER_ID, NEW_IMAGE_ID, "teams-ingress:active")
    lock_services = {
        "github-issues": "github-issues-provider",
        "exchange": "exchange",
        "navigator": "dsh-executor",
    }
    active_services = {"navigator": navigator, "teams-ingress": ingress}
    for index, target in enumerate(targets, start=3):
        if target not in active_services:
            active_services[target] = _active_service(
                f"{index:064x}", "sha256:" + f"{index + 10:064x}", f"{target}:active"
            )
    plan: dict[str, Any] = {
        "schemaVersion": runtime_update.PLAN_SCHEMA,
        "operationId": OPERATION_ID,
        "createdAtUtc": "2026-10-26T00:00:00+00:00",
        "project": "test-project",
        "targets": sorted(targets),
        "activeInputs": {
            "composeFiles": [{"path": "/private/active-compose.yaml", "sha256": "a" * 64}],
            "envFiles": [{"path": "/private/active.env", "sha256": "b" * 64}],
            "imageLock": {"path": "/private/active-lock.json", "sha256": "c" * 64},
        },
        "candidateInputs": {
            "composeFiles": [{"path": "/private/candidate-compose.yaml", "sha256": "d" * 64}],
            "envFiles": [{"path": "/private/candidate.env", "sha256": "e" * 64}],
            "imageLock": {"path": "/private/candidate-lock.json", "sha256": "f" * 64},
        },
        "activeSourceIdentitySha256": "1" * 64,
        "candidateSourceIdentitySha256": "2" * 64,
        "candidateImages": {
            target: {
                "lockService": lock_services[target],
                "image": f"{target}:candidate",
                "imageId": NEW_IMAGE_ID,
                "sourceCommits": {},
            }
            for target in targets
        },
        "activeServices": active_services,
        "edgeDelivery": {
            "service": "teams-ingress",
            "containerId": ingress["containerId"],
            "imageRef": ingress["imageRef"],
            "imageId": ingress["imageId"],
            "revision": 0,
            "deadCount": 0,
        },
        "navigatorDrain": {
            "containerId": navigator["containerId"],
            "imageRef": navigator["imageRef"],
            "imageId": navigator["imageId"],
            "instanceId": "navigator-instance",
            "generation": 0,
        },
        "policy": {
            "scope": "standalone-teams",
            "managedRuntimeMaintenance": "UNMANAGED_REQUIRED",
            "targetsOnly": targets_only,
            "updateOrder": update_order,
            "edgeImageReplacement": False,
            "composeNoDeps": True,
            "dockerPrefix": ["docker"],
            "drainTimeoutSeconds": 300,
            "healthTimeoutSeconds": 180,
        },
        "preflight": {
            "mode": "stage1",
            "activeDeploymentReady": True,
            "candidateDeploymentReady": True,
            "secretsDir": "/private/secrets",
            "configDir": "/private/config",
            "maxMemoryMiB": 3584,
            "maxCpus": 1.95,
            "requiredFreeGiB": 2.0,
            "minimumMemAvailableMiB": 1024,
        },
        "planSha256": "0" * 64,
    }
    plan["planSha256"] = runtime_update._plan_digest(plan)
    return plan


def _merge_compose_mapping(left: dict[str, Any], right: dict[str, Any]) -> None:
    """Merge YAML mappings for a bounded `docker compose config` subprocess stub."""

    for key, value in right.items():
        current = left.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_compose_mapping(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            current.extend(item for item in value if item not in current)
        else:
            left[key] = copy.deepcopy(value)


def _render_actual_runtime_compose(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Render tracked Compose and ACP/computer/cloud overlays through a command stub."""

    deployment_root = Path(__file__).resolve().parents[1]
    compose_files = [
        deployment_root / "compose.yaml",
        deployment_root / "compose.codebuddy-acp.yaml",
        deployment_root / "compose.computer-runtime.yaml",
        deployment_root / "compose.cloud-phase2.yaml",
        deployment_root / "compose.runpod.yaml",
    ]

    def compose_config(argv: list[str], *, capture: bool = True) -> str:
        assert capture is True
        selected_files = [
            Path(argv[index + 1])
            for index, argument in enumerate(argv[:-1])
            if argument == "--file"
        ]
        active_profiles = {
            argv[index + 1] for index, argument in enumerate(argv[:-1]) if argument == "--profile"
        }
        assert selected_files == compose_files
        assert runtime_update.preflight.ACTIVE_COMPOSE_PROFILES == (
            "public-ingress",
            "cloud-phase2",
            "runpod",
            "directory-operator",
        )
        assert active_profiles == set(runtime_update.preflight.ACTIVE_COMPOSE_PROFILES)
        assert argv[-3:] == ["config", "--format", "json"]

        rendered: dict[str, Any] = {}
        for path in selected_files:
            _merge_compose_mapping(rendered, yaml.safe_load(path.read_text(encoding="utf-8")))
        services = rendered.get("services")
        assert isinstance(services, dict)
        rendered["services"] = {
            name: service
            for name, service in services.items()
            if not service.get("profiles") or active_profiles.intersection(service["profiles"])
        }
        return json.dumps(rendered)

    monkeypatch.setattr(runtime_update.preflight, "_run", compose_config)
    return runtime_update.preflight._compose_config(["docker"], compose_files, [])


def _locked_compose_service_rows(
    compose: dict[str, Any], service_names: set[str]
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """Build exact image-lock and Docker inspection data for selected Compose services."""

    image_rows: list[dict[str, str]] = []
    container_ids: list[str] = []
    inspect_rows: list[dict[str, Any]] = []
    services = compose["services"]
    for index, service_name in enumerate(sorted(service_names), start=1):
        service = services[service_name]
        image_ref = f"example.test/{service_name}:runtime-update-test"
        image_id = "sha256:" + f"{index:064x}"
        container_id = f"{index:064x}"
        service["image"] = image_ref
        image_rows.append({"image": image_ref, "imageId": image_id})
        container_ids.append(container_id)
        inspect_rows.append(
            {
                "Id": container_id,
                "Image": image_id,
                "Config": {
                    "Image": image_ref,
                    "Labels": {
                        "com.docker.compose.project": compose["name"],
                        "com.docker.compose.service": service_name,
                        **{key: str(value) for key, value in service.get("labels", {}).items()},
                    },
                },
                "State": {
                    "Running": True,
                    **({"Health": {"Status": "healthy"}} if service.get("healthcheck") else {}),
                },
                "NetworkSettings": {"Networks": {"private": {"NetworkID": "network-id"}}},
            }
        )
    return {"images": image_rows}, container_ids, inspect_rows


def _stub_project_container_readback(
    monkeypatch: pytest.MonkeyPatch,
    container_ids: list[str],
    inspect_rows: list[dict[str, Any]],
) -> None:
    """Stub only the read-only Docker inventory commands used by the updater."""

    def checked_stdout(argv: list[str], *, label: str, timeout_seconds: float = 30.0) -> str:
        assert argv[0] == "docker"
        if argv[1] == "ps":
            assert label == "running project inventory"
            return "\n".join(container_ids)
        assert argv[1] == "inspect" and label == "service inspection"
        return json.dumps(inspect_rows)

    monkeypatch.setattr(runtime_update, "_checked_stdout", checked_stdout)


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


def test_runtime_plan_maps_github_compose_service_to_canonical_provider_lock_row() -> None:
    """Map `github-issues` Compose to the image-lock `github-issues-provider` identity."""

    compose = {"services": {"github-issues": {"image": "github-issues:candidate"}}}
    lock = {
        "images": [
            {
                "service": "github-issues-provider",
                "image": "github-issues:candidate",
                "imageId": NEW_IMAGE_ID,
                "sourceCommits": {"plugins": "b" * 40},
            }
        ]
    }

    assert runtime_update._target_images(compose, lock, ["github-issues"]) == {
        "github-issues": {
            "lockService": "github-issues-provider",
            "image": "github-issues:candidate",
            "imageId": NEW_IMAGE_ID,
            "sourceCommits": {"plugins": "b" * 40},
        }
    }


def test_runtime_plan_validation_preserves_legacy_nav_exchange_v1_policy() -> None:
    """Accept old target policies while validating the new GitHub provider contract."""

    legacy_plan = _reviewed_plan(
        ["exchange", "navigator"],
        targets_only=runtime_update.LEGACY_TARGETS_ONLY,
        update_order=["exchange", "navigator"],
    )
    runtime_update._validate_plan(legacy_plan)

    provider_plan = _reviewed_plan(
        ["github-issues"],
        targets_only=list(runtime_update.TARGET_ORDER),
        update_order=["github-issues"],
    )
    runtime_update._validate_plan(provider_plan)

    combined_plan = _reviewed_plan(
        ["github-issues", "navigator"],
        targets_only=list(runtime_update.TARGET_ORDER),
        update_order=["github-issues", "navigator"],
    )
    runtime_update._validate_plan(combined_plan)

    provider_claim_using_legacy_policy = _reviewed_plan(
        ["github-issues"],
        targets_only=runtime_update.LEGACY_TARGETS_ONLY,
        update_order=["github-issues"],
    )
    with pytest.raises(runtime_update.RuntimeUpdateError, match="outside the supported contract"):
        runtime_update._validate_plan(provider_claim_using_legacy_policy)


def test_edge_status_accepts_plugins_nonempty_queue_and_epoch_times() -> None:
    """Validate the float timestamps emitted by the pinned Teams DeliveryStatus producer."""

    response = {"_httpStatus": 200, **_plugins_delivery_status()}

    runtime_update._validate_edge_status(response)

    assert response["queued_count"] == 1
    assert response["delivered_count"] == 1
    assert response["oldest_pending_at"] == 1792991936.462
    assert response["last_delivered_at"] == 1792991801.25
    assert isinstance(response["oldest_pending_at"], float)
    assert isinstance(response["last_delivered_at"], float)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oldest_pending_at", True),
        ("last_delivered_at", "1792991801.25"),
        ("oldest_pending_at", -0.25),
        ("last_delivered_at", float("inf")),
        ("oldest_pending_at", float("nan")),
    ],
)
def test_edge_status_rejects_invalid_plugins_timestamp_values(field: str, value: Any) -> None:
    """Reject nonnumeric, boolean, negative, or non-finite timestamp values."""

    response = {"_httpStatus": 200, **_plugins_delivery_status()}
    response[field] = value

    with pytest.raises(runtime_update.RuntimeUpdateError, match="timestamps are malformed"):
        runtime_update._validate_edge_status(response)


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
            "--target",
            "github-issues",
            "--output",
            "/private/.artifacts/plan.json",
        ]
    )

    assert arguments.docker_prefix == "sudo-n-docker"
    assert arguments.target == ["navigator", "github-issues"]


def test_apply_global_project_lock_cannot_be_bypassed_with_another_journal_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Serialize concurrent apply calls even when they choose separate journal paths."""

    plan = {
        "operationId": OPERATION_ID,
        "project": "runtime-update-lock-test",
        "policy": {"dockerPrefix": ["docker"]},
    }
    journal_dir_a = tmp_path / "journal-a"
    journal_dir_b = tmp_path / "journal-b"
    journal_dir_a.mkdir(mode=0o700)
    journal_dir_b.mkdir(mode=0o700)
    lock_directory = tmp_path / "project-locks"
    lock_directory.mkdir(mode=0o700)
    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_DIRECTORY", lock_directory)
    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_OWNER_UID", os.geteuid())
    entered_preflight = threading.Event()
    release_preflight = threading.Event()
    first_errors: list[BaseException] = []
    monkeypatch.setattr(runtime_update, "_load_plan_file", lambda *_args: plan)

    def hold_first_apply(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        entered_preflight.set()
        if not release_preflight.wait(timeout=5):
            raise AssertionError("test did not release first plan verification")
        raise runtime_update.RuntimeUpdateError("stop before any external update")

    monkeypatch.setattr(runtime_update, "_resolve_plan_inputs", hold_first_apply)

    def first_apply() -> None:
        try:
            runtime_update.apply_plan_file(
                Path("ignored-plan.json"),
                expected_digest="d" * 64,
                journal_dir=journal_dir_a,
            )
        except runtime_update.RuntimeUpdateError as error:
            first_errors.append(error)

    thread = threading.Thread(target=first_apply)
    thread.start()
    assert entered_preflight.wait(timeout=2), first_errors
    with pytest.raises(runtime_update.RuntimeUpdateError, match="another runtime update"):
        runtime_update.apply_plan_file(
            Path("ignored-plan.json"),
            expected_digest="d" * 64,
            journal_dir=journal_dir_b,
        )
    release_preflight.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(first_errors) == 1
    assert isinstance(first_errors[0], runtime_update.RuntimeUpdateError)
    lock_metadata = runtime_update._operation_lock_path(plan["project"]).stat()
    assert lock_metadata.st_uid == os.geteuid()
    assert runtime_update.stat.S_IMODE(lock_metadata.st_mode) == 0o600
    assert lock_metadata.st_nlink == 1


def test_runtime_update_requires_the_fixed_root_operator(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a second host identity instead of letting it acquire an independent lock."""

    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_OWNER_UID", os.geteuid() + 1)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="must run as root"):
        runtime_update._require_update_operator()

    assert (
        runtime_update.main(
            ["apply", "--plan", "/missing/plan.json", "--confirm-plan-sha256", "d" * 64]
        )
        == 3
    )
    assert "must run as root" in capsys.readouterr().err


def test_project_lock_rejects_symlink_and_hard_link_replacements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep the root-private lock inode pinned against untrusted path substitution."""

    lock_directory = tmp_path / "locks"
    lock_directory.mkdir(mode=0o700)
    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_DIRECTORY", lock_directory)
    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_OWNER_UID", os.geteuid())
    lock_path = runtime_update._operation_lock_path("unsafe-lock-test")
    target = tmp_path / "target"
    target.write_text("", encoding="ascii")
    target.chmod(0o600)
    lock_path.symlink_to(target)
    with (
        pytest.raises(runtime_update.RuntimeUpdateError),
        runtime_update._operation_lock("unsafe-lock-test"),
    ):
        pytest.fail("symlinked lock was accepted")

    lock_path.unlink()
    lock_path.write_text("", encoding="ascii")
    lock_path.chmod(0o600)
    second_link = tmp_path / "second-link"
    second_link.hardlink_to(lock_path)
    with (
        pytest.raises(runtime_update.RuntimeUpdateError, match="mode 0600"),
        runtime_update._operation_lock("unsafe-lock-test"),
    ):
        pytest.fail("multiply linked lock was accepted")


def test_unresolved_stop_and_old_image_restart_intents_require_manual_reconciliation() -> None:
    """Only a persisted Docker command completion can close a pre-call restart intent."""

    stop_intent = {
        "phase": "NAVIGATOR_STOP_REQUESTED",
        "navigator": {"stop": {"containerId": OLD_CONTAINER_ID}},
        "targetProgress": {},
    }
    assert runtime_update._has_unresolved_docker_mutation(stop_intent)
    stop_intent["navigator"]["stop"].update(
        commandExitCode=0,
        exitCode=0,
        graceful=True,
        startedAt="2026-10-26T04:00:00Z",
        finishedAt="2026-10-26T04:01:00Z",
    )
    assert not runtime_update._has_unresolved_docker_mutation(stop_intent)

    restart_intent = {
        "phase": "NAVIGATOR_ACTIVE_IMAGE_RESTART_REQUESTED",
        "navigator": {
            "restartIntent": {
                "kind": "active-image-restart",
                "composeCommandExitCode": 0,
                "composeCommandReturnedAtUtc": "2026-10-26T04:02:00+00:00",
            }
        },
        "targetProgress": {},
    }
    assert not runtime_update._has_unresolved_docker_mutation(restart_intent)
    restart_intent["navigator"]["restartIntent"].pop("composeCommandReturnedAtUtc")
    assert runtime_update._has_unresolved_docker_mutation(restart_intent)


def test_current_compose_has_the_exact_private_navigator_exchange_caller_set() -> None:
    """Render-shape regression: only Navigator may call Exchange on its business network."""

    deployment_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((deployment_root / "compose.yaml").read_text())

    runtime_update._validate_standalone_callers(compose)

    with_extra_caller = copy.deepcopy(compose)
    with_extra_caller["services"]["unapproved-caller"] = {
        "networks": ["navigator-exchange-private"]
    }
    with pytest.raises(runtime_update.RuntimeUpdateError, match="unsupported service consumer"):
        runtime_update._validate_standalone_callers(with_extra_caller)


def test_runtime_inventory_uses_real_stage1_selector_for_codebuddy_computer_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render tracked Compose overlays and require exactly the twelve Stage 1 services."""

    compose = _render_actual_runtime_compose(monkeypatch)
    expected = runtime_update.preflight._expected_stage1_services(compose)
    expected_names = {
        "postgres",
        "workspace-authority",
        "account-center",
        "navigator-persistence",
        "navigator",
        "exchange",
        "github-issues",
        "teams-provider",
        "teams-ingress",
        "teams-public-ingress",
        "computer-runtime",
        "computer-runtime-proxy",
    }
    assert set(expected) == expected_names
    assert compose["services"]["navigator"]["labels"]["com.cyrene.runtime.profile"] == (
        "codebuddy-acp"
    )
    assert "cloud-management-phase2" in compose["services"]
    assert "cloud-management-phase2" not in expected
    assert "directory-operator" in compose["services"]
    assert "directory-operator" not in expected
    assert "directory-migrate" not in compose["services"]
    assert "huggingface" not in compose["services"]

    with_unapproved_service = copy.deepcopy(compose)
    with_unapproved_service["services"]["unapproved-sidecar"] = {
        "image": "example.test/unapproved-sidecar:test",
        "restart": "unless-stopped",
    }
    with pytest.raises(
        runtime_update.preflight.PreflightError,
        match="unsupported active Stage 1 Compose service: unapproved-sidecar",
    ):
        runtime_update.preflight._expected_stage1_services(with_unapproved_service)

    incomplete_computer_pair = copy.deepcopy(compose)
    del incomplete_computer_pair["services"]["computer-runtime-proxy"]
    with pytest.raises(
        runtime_update.preflight.PreflightError,
        match="computer runtime and proxy must be configured together",
    ):
        runtime_update.preflight._expected_stage1_services(incomplete_computer_pair)

    image_lock, container_ids, inspect_rows = _locked_compose_service_rows(compose, expected_names)
    _stub_project_container_readback(monkeypatch, container_ids, inspect_rows)

    actual = runtime_update._inspect_project_services(
        compose=compose,
        image_lock=image_lock,
        docker_prefix=["docker"],
    )

    assert set(actual) == expected_names
    assert {name for name in actual if name.startswith("computer-runtime")} == {
        "computer-runtime",
        "computer-runtime-proxy",
    }

    without_acp = copy.deepcopy(compose)
    without_acp["services"]["navigator"]["labels"].pop("com.cyrene.runtime.profile")
    with pytest.raises(runtime_update.RuntimeUpdateError, match="requires the CodeBuddy ACP"):
        runtime_update._inspect_project_services(
            compose=without_acp,
            image_lock=image_lock,
            docker_prefix=["docker"],
        )

    navigator_row = next(
        row
        for row in inspect_rows
        if row["Config"]["Labels"]["com.docker.compose.service"] == "navigator"
    )
    navigator_row["Config"]["Labels"]["com.cyrene.runtime.profile"] = "other-profile"
    with pytest.raises(runtime_update.RuntimeUpdateError, match="does not match the rendered"):
        runtime_update._inspect_project_services(
            compose=compose,
            image_lock=image_lock,
            docker_prefix=["docker"],
        )


@pytest.mark.parametrize(
    ("case", "extra_services"),
    [
        ("inactive-later-stage-profile", {"cloud-management-phase2"}),
        ("one-shot-operator", {"directory-operator"}),
        ("inactive-computer-profile", {"computer-runtime", "computer-runtime-proxy"}),
    ],
)
def test_runtime_inventory_rejects_running_services_outside_stage1_selection(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    extra_services: set[str],
) -> None:
    """Fail closed for later profiles, one-shot jobs, and configured inactive overlays."""

    compose = _render_actual_runtime_compose(monkeypatch)
    if case == "inactive-computer-profile":
        for service_name in extra_services:
            compose["services"][service_name]["profiles"] = ["computer-runtime"]
    expected = runtime_update.preflight._expected_stage1_services(compose)
    inventory_names = set(expected) | extra_services
    image_lock, container_ids, inspect_rows = _locked_compose_service_rows(compose, inventory_names)
    _stub_project_container_readback(monkeypatch, container_ids, inspect_rows)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="unexpected running worker"):
        runtime_update._inspect_project_services(
            compose=compose,
            image_lock=image_lock,
            docker_prefix=["docker"],
        )


def test_runtime_maintenance_static_markers_fail_closed() -> None:
    """Reject even empty broker marker variables in rendered target services."""

    compose = {
        "services": {
            "navigator": {"environment": {"CYRENE_RUNTIME_ACTIVITY_SOURCE_ID": ""}},
            "exchange": {"environment": {}},
        }
    }

    with pytest.raises(runtime_update.RuntimeUpdateError, match="managed RuntimeMaintenance"):
        runtime_update._validate_runtime_activity_static(compose)


@pytest.mark.parametrize("marker", sorted(runtime_update.RUNTIME_ACTIVITY_MARKERS))
def test_runtime_maintenance_static_detector_checks_each_marker(
    marker: str,
) -> None:
    """Keep every broker integration marker out of both target Compose environments."""

    compose = {
        "services": {
            "navigator": {"environment": {}},
            "exchange": {"environment": {marker: ""}},
        }
    }

    with pytest.raises(runtime_update.RuntimeUpdateError, match="managed RuntimeMaintenance"):
        runtime_update._validate_runtime_activity_static(compose)


def test_runtime_maintenance_probe_reports_presence_without_paths_or_token_values(
    tmp_path: Path,
) -> None:
    """Check default/custom presence detection while keeping values out of its output."""

    token_path = tmp_path / "private-runtime-token"
    token_value = "do-not-print-runtime-token"
    token_path.write_text(token_value, encoding="utf-8")
    socket_path = tmp_path / "private-maintenance.sock"
    socket_path.touch()

    def probe(env: dict[str, str]) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, "-c", runtime_update._runtime_activity_probe_script()],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=5,
        )
        assert result.returncode == 0
        assert token_value not in result.stdout
        assert str(token_path) not in result.stdout
        assert str(socket_path) not in result.stdout
        return json.loads(result.stdout)

    base_env = {
        key: value
        for key, value in os.environ.items()
        if key not in runtime_update.RUNTIME_ACTIVITY_MARKERS
        and key
        not in {
            "CYRENE_RUNTIME_MAINTENANCE_SOCKET",
            "CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE",
        }
    }
    assert probe(base_env) == {
        "managed": False,
        "markerCount": 0,
        "tokenFilePresent": False,
        "socketPresent": False,
    }
    assert probe(
        {
            **base_env,
            "CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE": str(token_path),
            "CYRENE_RUNTIME_MAINTENANCE_SOCKET": str(socket_path),
        }
    ) == {
        "managed": True,
        "markerCount": 2,
        "tokenFilePresent": True,
        "socketPresent": True,
    }
    assert probe({**base_env, "CYRENE_RUNTIME_ACTIVITY_SOURCE_ID": ""}) == {
        "managed": True,
        "markerCount": 1,
        "tokenFilePresent": False,
        "socketPresent": False,
    }


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


def test_recorded_navigator_stop_is_read_back_before_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not trust an old graceful-stop journal if Docker restarted that container."""

    stopped = {
        "Status": "exited",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "OOMKilled": False,
        "ExitCode": 0,
        "Error": "",
        "StartedAt": "2026-10-26T04:00:00Z",
        "FinishedAt": "2026-10-26T04:01:00Z",
    }
    journal = {
        "navigator": {
            "drained": True,
            "stop": {
                "containerId": OLD_CONTAINER_ID,
                "graceful": True,
                "startedAt": stopped["StartedAt"],
                "finishedAt": stopped["FinishedAt"],
            },
        }
    }
    monkeypatch.setattr(runtime_update, "_inspect_container_state", lambda *_args: stopped)

    runtime_update._stop_drained_navigator(
        plan={},
        context={},
        journal=journal,
        journal_path=Path("unused"),
        docker_prefix=["docker"],
        container_id=OLD_CONTAINER_ID,
    )

    monkeypatch.setattr(
        runtime_update,
        "_inspect_container_state",
        lambda *_args: {**stopped, "Status": "running", "Running": True},
    )
    with pytest.raises(runtime_update.RuntimeUpdateError, match="read-back changed"):
        runtime_update._stop_drained_navigator(
            plan={},
            context={},
            journal=journal,
            journal_path=Path("unused"),
            docker_prefix=["docker"],
            container_id=OLD_CONTAINER_ID,
        )


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


def test_compose_timeout_is_classified_as_an_unknown_daemon_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat a timed-out mutating Compose call as unsettled even when the client vanished."""

    monkeypatch.setattr(
        runtime_update.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["docker", "compose", "up"], timeout=1)
        ),
    )

    with pytest.raises(
        runtime_update.RuntimeUpdateOutcomeUnknown, match="daemon outcome is unknown"
    ):
        runtime_update._compose_up(
            docker_prefix=["docker"],
            project="test-project",
            compose_files=[Path("compose.yaml")],
            env_files=[Path("runtime.env")],
            service="exchange",
            timeout_seconds=1,
        )


def test_github_provider_compose_update_is_no_deps_and_targets_only_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update only the provider, preserving Account Center and Exchange containers."""

    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(runtime_update, "_run_command", run)
    runtime_update._compose_up(
        docker_prefix=["docker"],
        project="cyrene-teams-cloud",
        compose_files=[Path("compose.yaml"), Path("compose.codebuddy-acp.yaml")],
        env_files=[Path("runtime.env"), Path("images.env")],
        service="github-issues",
        timeout_seconds=90,
    )

    argv = calls[0]
    assert argv[-1] == "github-issues"
    assert "--no-deps" in argv
    assert "--pull" in argv and argv[argv.index("--pull") + 1] == "never"
    assert "--no-build" in argv
    assert "account-center" not in argv
    assert "exchange" not in argv
    profiles = [
        argv[index + 1] for index, argument in enumerate(argv[:-1]) if argument == "--profile"
    ]
    assert profiles == list(runtime_update.preflight.ACTIVE_COMPOSE_PROFILES)
    assert "directory-bootstrap" not in profiles
    assert argv[argv.index("up") + 1 :] == [
        "--detach",
        "--no-deps",
        "--pull",
        "never",
        "--no-build",
        "github-issues",
    ]


def test_github_provider_target_uses_locked_image_health_readback_without_nav_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Replace and verify only GitHub provider while the shared transaction keeps Nav drained."""

    old_provider = _active_service(OLD_CONTAINER_ID, OLD_IMAGE_ID, "github-issues:active")
    new_provider = _active_service(NEW_CONTAINER_ID, NEW_IMAGE_ID, "github-issues:candidate")
    plan = {
        "project": "cyrene-teams-cloud",
        "targets": ["github-issues"],
        "activeServices": {"github-issues": old_provider},
        "candidateImages": {
            "github-issues": {
                "lockService": "github-issues-provider",
                "image": new_provider["imageRef"],
                "imageId": new_provider["imageId"],
                "sourceCommits": {"plugins": "b" * 40},
            }
        },
        "candidateInputs": {"composeFiles": [], "envFiles": []},
        "policy": {"healthTimeoutSeconds": 30},
    }
    journal = {
        "updatedTargets": [],
        "targetProgress": {},
        "phase": "NAVIGATOR_DRAINED",
        "updatedAtUtc": "2026-10-26T00:00:00+00:00",
    }
    service_states = iter([{"github-issues": old_provider}, {"github-issues": new_provider}])
    compose_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runtime_update, "_operation_service_state", lambda **_kwargs: next(service_states)
    )
    monkeypatch.setattr(runtime_update, "_verify_runtime_unmanaged", lambda *_args: None)
    monkeypatch.setattr(runtime_update, "_input_paths", lambda *_args: [Path("candidate.yaml")])
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)
    monkeypatch.setattr(
        runtime_update,
        "_set_phase",
        lambda target_journal, _path, phase, **_kwargs: target_journal.update({"phase": phase}),
    )
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **kwargs: compose_calls.append(kwargs) or subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(
        runtime_update,
        "_stop_drained_navigator",
        lambda **_kwargs: pytest.fail("provider target must not stop Navigator"),
    )

    runtime_update._update_target(
        plan=plan,
        context={},
        journal=journal,
        journal_path=tmp_path / "provider-update.json",
        docker_prefix=["docker"],
        target="github-issues",
    )

    assert len(compose_calls) == 1
    assert compose_calls[0]["service"] == "github-issues"
    assert journal["updatedTargets"] == ["github-issues"]
    assert journal["targetProgress"]["github-issues"]["phase"] == "UPDATED_HEALTHY"
    assert journal["targetProgress"]["github-issues"]["newContainerId"] == NEW_CONTAINER_ID


def test_github_provider_rollback_restores_only_its_locked_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Rollback a provider candidate under the same Nav drain and ingress pause."""

    old_provider = _active_service(OLD_CONTAINER_ID, OLD_IMAGE_ID, "github-issues:active")
    candidate_provider = _active_service(NEW_CONTAINER_ID, NEW_IMAGE_ID, "github-issues:candidate")
    navigator = _active_service("3" * 64, "sha256:" + "c" * 64, "navigator:active")
    plan = {
        "project": "cyrene-teams-cloud",
        "targets": ["github-issues"],
        "activeServices": {"github-issues": old_provider, "navigator": navigator},
        "candidateImages": {
            "github-issues": {
                "lockService": "github-issues-provider",
                "image": candidate_provider["imageRef"],
                "imageId": candidate_provider["imageId"],
                "sourceCommits": {"plugins": "b" * 40},
            }
        },
        "activeInputs": {"composeFiles": [], "envFiles": []},
        "policy": {"drainTimeoutSeconds": 60, "healthTimeoutSeconds": 30},
    }
    journal = {
        "targets": ["github-issues"],
        "updatedTargets": ["github-issues"],
        "targetProgress": {
            "github-issues": {
                "phase": "UPDATED_HEALTHY",
                "oldContainerId": OLD_CONTAINER_ID,
                "oldImageId": OLD_IMAGE_ID,
                "candidateImageId": NEW_IMAGE_ID,
                "newContainerId": NEW_CONTAINER_ID,
            }
        },
        "edge": {"latestDeadCount": 0},
        "navigator": {"stop": None},
    }
    states = iter(
        [
            {"github-issues": candidate_provider, "navigator": navigator},
            {"github-issues": candidate_provider, "navigator": navigator},
            {"github-issues": old_provider, "navigator": navigator},
            {"github-issues": old_provider, "navigator": navigator},
        ]
    )
    compose_calls: list[dict[str, Any]] = []
    navigator_drains: list[str] = []
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_recover_edge_pause", lambda **_kwargs: None)
    monkeypatch.setattr(
        runtime_update, "_recover_old_navigator_before_candidate_start", lambda **_kwargs: False
    )
    monkeypatch.setattr(runtime_update, "_read_transition_state", lambda **_kwargs: next(states))
    monkeypatch.setattr(
        runtime_update,
        "_drain_current_navigator_for_rollback",
        lambda **_kwargs: navigator_drains.append("drained"),
    )
    monkeypatch.setattr(runtime_update, "_verify_runtime_unmanaged", lambda *_args: None)
    monkeypatch.setattr(runtime_update, "_input_paths", lambda *_args: [Path("active.yaml")])
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **kwargs: compose_calls.append(kwargs) or subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(runtime_update, "_verify_live_network_membership", lambda *_args: None)
    monkeypatch.setattr(runtime_update, "_resume_navigator_after_rollback", lambda **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_resume_edge", lambda **_kwargs: None)

    runtime_update._rollback_operation(
        plan=plan,
        context={"activeCompose": {}, "activeLock": {}},
        journal=journal,
        journal_path=tmp_path / "provider-rollback.json",
        docker_prefix=["docker"],
    )

    assert navigator_drains == ["drained"]
    assert len(compose_calls) == 1
    assert compose_calls[0]["service"] == "github-issues"
    assert journal["updatedTargets"] == []
    assert journal["targetProgress"]["github-issues"]["phase"] == "ROLLED_BACK"


def test_internal_status_validation_preserves_the_json_document() -> None:
    """Keep native Navigator drain fields available after removing the HTTP envelope."""

    response = {"_httpStatus": 200, **_native_navigator_status("accepting")}
    response["queuedTasks"] = 2

    runtime_update._validate_navigator_status(response)

    assert response["mode"] == "accepting"
    assert response["queuedTasks"] == 2
    assert response["countsComplete"] is True
    assert response["dispatches"] == {"active": 0, "waiting": 0}
    assert "_httpStatus" not in response


def test_final_delivery_readback_consumes_current_status_http_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the real GET/status helper and validator through the final APPLIED check."""

    document = _edge_status(paused=False, revision=2)
    token_path = "/run/secrets/teams-delivery-admin-token"
    token_source = tmp_path / "delivery-admin-token"
    token_source.write_text("test-only-token", encoding="ascii")
    token_source.chmod(0o400)
    metadata = _internal_probe_container(
        container_id=OLD_CONTAINER_ID,
        service="teams-ingress",
        token_path=token_path,
        token_source=token_source,
    )

    def run_command(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        url = next(
            value for value in argv if isinstance(value, str) and value.startswith("http://")
        )
        assert url.endswith("/_internal/teams/delivery/status")
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"httpStatus": 200, "document": document}),
            "",
        )

    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: _internal_probe_inspect_output(metadata),
    )
    monkeypatch.setattr(runtime_update, "_run_command", run_command)
    journal = {
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "resumeRevision": 2,
        }
    }

    result = runtime_update._verify_final_edge_delivery_readback(
        journal=journal,
        docker_prefix=["docker"],
    )

    assert result["paused"] is False
    assert result["revision"] == 2
    assert "_httpStatus" not in result


@pytest.mark.parametrize(
    "status_name", ["accepting", "drainingActive", "drained", "resumedPolling"]
)
def test_navigator_drain_validation_accepts_native_wire_fixture(status_name: str) -> None:
    """Validate the complete JSON shape emitted by Navigator 18baefba, including resumed polling."""

    response = {"_httpStatus": 200, **_native_navigator_status(status_name)}

    runtime_update._validate_navigator_status(response)

    assert response["countsComplete"] is True
    assert set(response["dispatches"]) == {"active", "waiting"}
    assert set(response["notifications"]) == {
        "configured",
        "pollingPaused",
        "inFlight",
    }
    if status_name == "resumedPolling":
        assert response["notifications"]["inFlight"] is True
        assert response["activeWorkDrained"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda status: status.update(countsComplete=False), "counts are incomplete"),
        (
            lambda status: status["dispatches"].update(waiting=1),
            "reported drained while active work remains",
        ),
        (
            lambda status: status["notifications"].pop("configured"),
            "notification drain status is malformed",
        ),
        (lambda status: status.update(startedAt="2026-10-26T00:00:00Z"), "timestamp is malformed"),
    ],
)
def test_navigator_drain_validation_rejects_missing_or_false_native_fences(
    mutation: Any, message: str
) -> None:
    """Fail closed when durable counts or process-local dispatch/notification fences are absent."""

    status = _native_navigator_status("drained")
    mutation(status)

    with pytest.raises(runtime_update.RuntimeUpdateError, match=message):
        runtime_update._validate_navigator_status({"_httpStatus": 200, **status})


def test_drain_waits_for_complete_counts_and_zero_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Do not stop Navigator until durable counts, dispatch slots, and notifications are idle."""

    accepting = _native_navigator_status("accepting")
    draining_active = _native_navigator_status("drainingActive")
    drained = _native_navigator_status("drained")
    statuses = [accepting, drained]
    calls: list[dict[str, Any]] = []

    def current_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        response = {"_httpStatus": 200, **copy.deepcopy(statuses.pop(0))}
        runtime_update._validate_navigator_status(response)
        return response

    def start_drain(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"_httpStatus": 200, **copy.deepcopy(draining_active)}

    monkeypatch.setattr(runtime_update, "_current_navigator_status", current_status)
    monkeypatch.setattr(runtime_update, "_call_internal_api", start_drain)
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)
    monkeypatch.setattr(runtime_update.time, "sleep", lambda _seconds: None)
    journal = {
        "navigator": {
            "initialGeneration": 0,
            "initialContainerId": OLD_CONTAINER_ID,
            "initialInstanceId": accepting["instanceId"],
            "drainAttempt": None,
        }
    }

    runtime_update._drain_navigator(
        plan={"policy": {"drainTimeoutSeconds": 60}},
        journal=journal,
        journal_path=tmp_path / "operation.json",
        docker_prefix=["docker"],
    )

    assert calls[0]["body"] == {}
    assert journal["navigator"]["drained"] is True
    assert journal["navigator"]["drainGeneration"] == 1
    assert statuses == []


def test_resume_uses_exact_native_generation_and_token_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Match the producer's closed resume DTO and allow notification polling to restart."""

    draining = _native_navigator_status("drained")
    resumed = _native_navigator_status("resumedPolling")
    request: dict[str, Any] = {}

    def current_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        response = {"_httpStatus": 200, **copy.deepcopy(draining)}
        runtime_update._validate_navigator_status(response)
        return response

    def resume(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        request.update(kwargs)
        return {"_httpStatus": 200, **copy.deepcopy(resumed)}

    monkeypatch.setattr(runtime_update, "_current_navigator_status", current_status)
    monkeypatch.setattr(runtime_update, "_call_internal_api", resume)

    response = runtime_update._resume_navigator_instance(
        docker_prefix=["docker"],
        container_id=OLD_CONTAINER_ID,
        instance_id=draining["instanceId"],
        generation=draining["generation"],
        timeout_seconds=30,
    )

    assert request["path"] == "/internal/v1/executor/drain/resume"
    assert request["body"] == {
        "generation": draining["generation"],
        "token": draining["token"],
    }
    assert response["mode"] == "accepting"
    assert response["notifications"]["inFlight"] is True
    assert response["activeWorkDrained"] is False


def test_internal_admin_http_bridge_rejects_redirects_without_forwarding_bearer(
    tmp_path: Path,
) -> None:
    """Never follow a 3xx response from a loopback admin endpoint with its bearer header."""

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.server.visited.append((self.path, self.command, self.headers.get("Authorization")))
            if self.path == "/internal/v1/executor/drain":
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{self.server.server_port}/capture",
                )
            else:
                self.send_response(200)
            self.end_headers()

        def do_GET(self) -> None:
            self.server.visited.append((self.path, self.command, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    server.visited = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_file = tmp_path / "admin-token"
    token_file.write_text("test-only-bearer", encoding="ascii")
    token_file.chmod(0o600)
    try:
        response = subprocess.run(
            [
                sys.executable,
                "-c",
                runtime_update._INTERNAL_HTTP_SCRIPT,
                str(token_file),
                f"http://127.0.0.1:{server.server_port}/internal/v1/executor/drain",
                "POST",
                "2",
            ],
            input="{}",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.returncode == 0
    assert json.loads(response.stdout)["httpStatus"] == 302
    assert server.visited == [("/internal/v1/executor/drain", "POST", "Bearer test-only-bearer")]
    assert "test-only-bearer" not in response.stdout


def test_internal_http_bridge_preserves_native_navigator_drain_fences(
    tmp_path: Path,
) -> None:
    """Forward every field from the native Navigator status to the validating consumer."""

    fixture = _native_navigator_status("drained")

    class NativeDrainHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.server.visited.append((self.path, self.headers.get("Authorization")))
            body = json.dumps(fixture, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), NativeDrainHandler)
    server.visited = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_file = tmp_path / "admin-token"
    token_file.write_text("test-only-bearer", encoding="ascii")
    token_file.chmod(0o600)
    try:
        response = subprocess.run(
            [
                sys.executable,
                "-c",
                runtime_update._INTERNAL_HTTP_SCRIPT,
                str(token_file),
                f"http://127.0.0.1:{server.server_port}/internal/v1/executor/drain",
                "GET",
                "2",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.returncode == 0
    bridge = json.loads(response.stdout)
    assert bridge["httpStatus"] == 200
    assert bridge["document"] == fixture
    assert server.visited == [("/internal/v1/executor/drain", "Bearer test-only-bearer")]
    assert "test-only-bearer" not in response.stdout


def test_internal_http_bridge_preserves_nonempty_plugins_spool_timestamps(
    tmp_path: Path,
) -> None:
    """Keep producer epoch-second numbers intact through the container-local admin bridge."""

    fixture = _plugins_delivery_status()

    class DeliveryStatusHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.server.visited.append((self.path, self.headers.get("Authorization")))
            body = json.dumps(fixture, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), DeliveryStatusHandler)
    server.visited = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_file = tmp_path / "admin-token"
    token_file.write_text("test-only-bearer", encoding="ascii")
    token_file.chmod(0o600)
    try:
        response = subprocess.run(
            [
                sys.executable,
                "-c",
                runtime_update._INTERNAL_HTTP_SCRIPT,
                str(token_file),
                f"http://127.0.0.1:{server.server_port}/_internal/teams/delivery/status",
                "GET",
                "2",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.returncode == 0
    bridge = json.loads(response.stdout)
    assert bridge["httpStatus"] == 200
    assert bridge["document"] == fixture
    assert isinstance(bridge["document"]["oldest_pending_at"], float)
    assert isinstance(bridge["document"]["last_delivered_at"], float)
    assert server.visited == [("/_internal/teams/delivery/status", "Bearer test-only-bearer")]
    assert "test-only-bearer" not in response.stdout


def test_internal_http_bridge_enforces_total_request_deadline(
    tmp_path: Path,
) -> None:
    """Exit the ephemeral probe process when a peer exceeds its total deadline."""

    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                time.sleep(1)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            except BrokenPipeError:
                return

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    token_file = tmp_path / "admin-token"
    token_file.write_text("test-only-bearer", encoding="ascii")
    token_file.chmod(0o600)
    started = time.monotonic()
    try:
        response = subprocess.run(
            [
                sys.executable,
                "-c",
                runtime_update._INTERNAL_HTTP_SCRIPT,
                str(token_file),
                f"http://127.0.0.1:{server.server_port}/_internal/teams/delivery/status",
                "GET",
                "0.2",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    assert response.returncode != 0
    assert response.stdout == ""
    assert time.monotonic() - started < 2.5


@pytest.mark.parametrize("service", ["teams-ingress", "navigator"])
def test_internal_status_probe_uses_locked_image_and_only_target_token_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
) -> None:
    """Launch a bounded sidecar on the target netns with one read-only token bind."""

    container_id = OLD_CONTAINER_ID
    token_path = (
        "/run/secrets/teams-delivery-admin-token"
        if service == "teams-ingress"
        else runtime_update.NAVIGATOR_TOKEN_FILE
    )
    token_source = tmp_path / f"{service}-admin-token"
    token_source.write_text("do-not-export-test-token", encoding="ascii")
    token_source.chmod(0o400)
    metadata = _internal_probe_container(
        container_id=container_id,
        service=service,
        token_path=token_path,
        token_source=token_source,
    )
    status_document = (
        _edge_status(paused=False, revision=7)
        if service == "teams-ingress"
        else _native_navigator_status("accepting")
    )
    inspect_calls: list[list[str]] = []
    run_calls: list[tuple[list[str], dict[str, Any]]] = []

    def inspect(argv: list[str], **_kwargs: Any) -> str:
        inspect_calls.append(argv)
        return _internal_probe_inspect_output(metadata)

    def run_command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"httpStatus": 200, "document": status_document}),
            "",
        )

    monkeypatch.setattr(runtime_update, "_checked_stdout", inspect)
    monkeypatch.setattr(runtime_update, "_run_command", run_command)
    path = (
        "/_internal/teams/delivery/status"
        if service == "teams-ingress"
        else "/internal/v1/executor/drain"
    )

    response = runtime_update._call_internal_api(
        ["docker"],
        container_id,
        service=service,
        method="GET",
        path=path,
        body=None,
        timeout_seconds=15,
    )

    assert len(inspect_calls) == 1
    assert inspect_calls[0][:3] == ["docker", "inspect", "--format"]
    assert inspect_calls[0][-1] == container_id
    assert ".Config.Env" not in inspect_calls[0][3]
    assert response["_httpStatus"] == 200
    assert response.get("paused", response.get("mode")) in {False, "accepting"}
    assert len(run_calls) == 1
    argv, options = run_calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--pull=never" in argv
    assert argv[argv.index("--network") + 1] == f"container:{container_id}"
    assert argv[argv.index("--entrypoint") + 1] == "python3"
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges:true"
    assert argv[argv.index("--memory") + 1] == "64m"
    assert argv[argv.index("--memory-swap") + 1] == "64m"
    assert argv[argv.index("--cpus") + 1] == "0.1"
    assert argv[argv.index("--pids-limit") + 1] == "32"
    assert argv[argv.index("--mount") + 1] == (
        f"type=bind,src={token_source.resolve()},dst={token_path},readonly"
    )
    assert argv.count("--mount") == 1
    image_index = argv.index(OLD_IMAGE_ID)
    assert argv[image_index + 1] == "-c"
    assert argv[-1] == "13.0"
    assert "--publish" not in argv and "-p" not in argv and "--label" not in argv
    assert options["timeout_seconds"] == 15
    assert options["external_mutation"] is False
    assert "do-not-export-test-token" not in repr(argv)
    assert "do-not-export-test-token" not in json.dumps(response)


def test_internal_control_post_http_200_returns_validated_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send one CAS request through the bounded sidecar and retain its HTTP envelope."""

    container_id = OLD_CONTAINER_ID
    token_path = "/run/secrets/teams-delivery-admin-token"
    token_source = tmp_path / "admin-token"
    token_source.write_text("test-only-token", encoding="ascii")
    token_source.chmod(0o400)
    metadata = _internal_probe_container(
        container_id=container_id,
        service="teams-ingress",
        token_path=token_path,
        token_source=token_source,
    )
    body = {"holder": OPERATION_ID, "expected_revision": 4}
    document = {"paused": True, "revision": 5}
    run_options: list[dict[str, Any]] = []

    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: _internal_probe_inspect_output(metadata),
    )

    def successful_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_options.append(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"httpStatus": 200, "document": document}),
            "",
        )

    monkeypatch.setattr(runtime_update, "_run_command", successful_probe)
    response = runtime_update._call_internal_api(
        ["docker"],
        container_id,
        service="teams-ingress",
        method="POST",
        path="/_internal/teams/delivery/pause",
        body=body,
        timeout_seconds=15,
    )

    assert response == {"paused": True, "revision": 5, "_httpStatus": 200}
    assert len(run_options) == 1
    assert run_options[0]["external_mutation"] is True
    assert run_options[0]["input_data"] == json.dumps(body, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate-token-mount", "missing or ambiguous"),
        ("writable-token-mount", "not a read-only file bind"),
        ("ambiguous-token-source", "not a read-only file bind"),
        ("wrong-service-label", "target identity is unavailable"),
        ("bad-image-id", "target identity is unavailable"),
        ("stopped-target", "target identity is unavailable"),
        ("host-network", "target identity is unavailable"),
    ],
)
def test_internal_status_probe_rejects_unverified_target_or_token_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    """Refuse ambiguous mounts, wrong target identity, and unsafe network state."""

    container_id = OLD_CONTAINER_ID
    token_path = "/run/secrets/teams-delivery-admin-token"
    token_source = tmp_path / (
        "admin,token" if mutation == "ambiguous-token-source" else "admin-token"
    )
    token_source.write_text("test-only-token", encoding="ascii")
    token_source.chmod(0o400)
    metadata = _internal_probe_container(
        container_id=container_id,
        service="teams-ingress",
        token_path=token_path,
        token_source=token_source,
    )
    if mutation == "duplicate-token-mount":
        metadata["Mounts"].append(copy.deepcopy(metadata["Mounts"][0]))
    elif mutation == "writable-token-mount":
        metadata["Mounts"][0]["RW"] = True
    elif mutation == "wrong-service-label":
        metadata["Config"]["Labels"]["com.docker.compose.service"] = "exchange"
    elif mutation == "bad-image-id":
        metadata["Image"] = "not-an-image-id"
    elif mutation == "stopped-target":
        metadata["State"]["Running"] = False
    elif mutation == "host-network":
        metadata["HostConfig"]["NetworkMode"] = "host"

    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: _internal_probe_inspect_output(metadata),
    )
    monkeypatch.setattr(
        runtime_update,
        "_run_command",
        lambda *_args, **_kwargs: pytest.fail("unsafe target must not start a probe container"),
    )
    with pytest.raises(runtime_update.RuntimeUpdateError, match=message):
        runtime_update._call_internal_api(
            ["docker"],
            container_id,
            service="teams-ingress",
            method="GET",
            path="/_internal/teams/delivery/status",
            body=None,
            timeout_seconds=15,
        )


def test_internal_control_post_timeout_remains_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate Docker run timeout as unknown so the caller cannot replay a POST."""

    container_id = OLD_CONTAINER_ID
    token_path = "/run/secrets/teams-delivery-admin-token"
    token_source = tmp_path / "admin-token"
    token_source.write_text("test-only-token", encoding="ascii")
    token_source.chmod(0o400)
    metadata = _internal_probe_container(
        container_id=container_id,
        service="teams-ingress",
        token_path=token_path,
        token_source=token_source,
    )
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: _internal_probe_inspect_output(metadata),
    )

    def timed_out(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(runtime_update.subprocess, "run", timed_out)
    with pytest.raises(
        runtime_update.RuntimeUpdateOutcomeUnknown, match="daemon outcome is unknown"
    ):
        runtime_update._call_internal_api(
            ["docker"],
            container_id,
            service="teams-ingress",
            method="POST",
            path="/_internal/teams/delivery/pause",
            body={"holder": OPERATION_ID, "expected_revision": 1},
            timeout_seconds=15,
        )


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (2, json.dumps({"bridgeError": "internal_request_failed"})),
        (0, json.dumps({"httpStatus": 503, "document": {}})),
    ],
)
def test_internal_control_post_failure_is_not_retried_or_reported_as_known_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
) -> None:
    """Treat bridge failure or non-success POST status as an unknown operation result."""

    container_id = OLD_CONTAINER_ID
    token_path = "/run/secrets/teams-delivery-admin-token"
    token_source = tmp_path / "admin-token"
    token_source.write_text("test-only-token", encoding="ascii")
    token_source.chmod(0o400)
    metadata = _internal_probe_container(
        container_id=container_id,
        service="teams-ingress",
        token_path=token_path,
        token_source=token_source,
    )
    run_options: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: _internal_probe_inspect_output(metadata),
    )

    def failed_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_options.append(kwargs)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(runtime_update, "_run_command", failed_probe)
    with pytest.raises(runtime_update.RuntimeUpdateOutcomeUnknown, match="outcome is unknown"):
        runtime_update._call_internal_api(
            ["docker"],
            container_id,
            service="teams-ingress",
            method="POST",
            path="/_internal/teams/delivery/pause",
            body={"holder": OPERATION_ID, "expected_revision": 1},
            timeout_seconds=15,
        )
    assert len(run_options) == 1
    assert run_options[0]["external_mutation"] is True


def test_lost_edge_resume_reply_reacquires_the_pause_with_a_fresh_cas_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Use a new holder after resume and replay it idempotently after a lost pause reply."""

    journal = {
        "operationId": OPERATION_ID,
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialRevision": 0,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "pauseHolder": OPERATION_ID,
            "pauseRevision": 1,
            "resumeRevision": 2,
            "pauseHistory": [1],
            "repauseExpectedRevision": None,
            "repauseHolder": None,
        },
        "phase": "EDGE_RESUME_REQUESTED",
        "state": "RUNNING",
        "history": [],
    }
    spool = {
        "paused": False,
        "revision": 2,
        "holder": None,
        "last_resumed_holder": OPERATION_ID,
    }
    calls: list[dict[str, Any]] = []
    lose_first_reply = True

    def current_edge_status(*_args: Any) -> dict[str, Any]:
        return _edge_status(paused=spool["paused"], revision=spool["revision"])

    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_current_edge_status", current_edge_status)

    def call_internal_api(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal lose_first_reply
        calls.append(kwargs["body"])
        body = kwargs["body"]
        if body["holder"] == spool["last_resumed_holder"]:
            raise runtime_update.RuntimeUpdateError("delivery CAS rejects a resumed holder")
        if spool["paused"]:
            if (
                body["holder"] != spool["holder"]
                or body["expected_revision"] != spool["revision"] - 1
            ):
                raise runtime_update.RuntimeUpdateError("delivery CAS holder/revision conflict")
        else:
            if body["expected_revision"] != spool["revision"]:
                raise runtime_update.RuntimeUpdateError("delivery CAS revision conflict")
            spool["paused"] = True
            spool["revision"] += 1
            spool["holder"] = body["holder"]
        response = {"_httpStatus": 200, **_edge_status(paused=True, revision=spool["revision"])}
        if lose_first_reply:
            lose_first_reply = False
            raise runtime_update.RuntimeUpdateError("pause committed but reply was lost")
        return response

    monkeypatch.setattr(runtime_update, "_call_internal_api", call_internal_api)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="rejects a resumed holder"):
        call_internal_api(body={"holder": OPERATION_ID, "expected_revision": 2})
    calls.clear()

    with pytest.raises(runtime_update.RuntimeUpdateError, match="reply was lost"):
        runtime_update._recover_edge_pause(
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
            timeout_seconds=60,
        )
    durable_repause_holder = journal["edge"]["repauseHolder"]
    assert durable_repause_holder != OPERATION_ID
    assert spool["paused"] is True and spool["revision"] == 3

    runtime_update._recover_edge_pause(
        journal=journal,
        journal_path=tmp_path / "operation.json",
        docker_prefix=["docker"],
        timeout_seconds=60,
    )

    assert calls == [
        {"holder": durable_repause_holder, "expected_revision": 2},
        {"holder": durable_repause_holder, "expected_revision": 2},
    ]
    assert journal["edge"]["pauseRevision"] == 3
    assert journal["edge"]["pauseHolder"] == durable_repause_holder
    assert journal["edge"]["resumeRevision"] is None
    assert journal["edge"]["pauseHistory"] == [1, 3]


def test_pause_cas_dead_letter_increase_is_recorded_and_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DLQ increment returned by the pause CAS cannot be reported as a successful pause."""

    journal = {
        "operationId": OPERATION_ID,
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialRevision": 0,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "pauseHolder": OPERATION_ID,
        },
    }
    pause_response = {
        "_httpStatus": 200,
        **_edge_status(paused=True, revision=1),
        "dead_count": 1,
    }
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args, **_kwargs: _edge_status(paused=False, revision=0),
    )
    monkeypatch.setattr(
        runtime_update, "_call_internal_api", lambda *_args, **_kwargs: pause_response
    )
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="pause CAS"):
        runtime_update._pause_edge(
            plan={"policy": {"drainTimeoutSeconds": 60}},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )
    assert journal["edge"]["latestDeadCount"] == 1


def test_pause_poll_dead_letter_increase_is_recorded_and_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DLQ increment during active-delivery drain aborts before any target update."""

    journal = {
        "operationId": OPERATION_ID,
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialRevision": 0,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "pauseHolder": OPERATION_ID,
        },
    }
    statuses = [
        _edge_status(paused=False, revision=0),
        _edge_status(paused=True, revision=1, active=0) | {"dead_count": 1},
    ]
    pause_response = {
        "_httpStatus": 200,
        **_edge_status(paused=True, revision=1, active=1),
    }
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args, **_kwargs: statuses.pop(0),
    )
    monkeypatch.setattr(
        runtime_update, "_call_internal_api", lambda *_args, **_kwargs: pause_response
    )
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)
    monkeypatch.setattr(runtime_update.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(runtime_update.time, "sleep", lambda _seconds: None)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="pause drain polling"):
        runtime_update._pause_edge(
            plan={"policy": {"drainTimeoutSeconds": 60}},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )
    assert journal["edge"]["latestDeadCount"] == 1


def test_pause_recovery_cas_dead_letter_increase_is_recorded_and_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A recovery pause CAS with a new DLQ entry stays an explicit recovery failure."""

    journal = {
        "operationId": OPERATION_ID,
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialRevision": 0,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "pauseHolder": OPERATION_ID,
            "pauseIntent": True,
            "pauseRevision": None,
            "resumeRevision": None,
        },
    }
    response = {
        "_httpStatus": 200,
        **_edge_status(paused=True, revision=1),
        "dead_count": 1,
    }
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args, **_kwargs: _edge_status(paused=False, revision=0),
    )
    monkeypatch.setattr(runtime_update, "_call_internal_api", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="pause recovery CAS"):
        runtime_update._recover_edge_pause(
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )
    assert journal["edge"]["latestDeadCount"] == 1
    assert journal["edge"]["resumeRevision"] is None


def test_resume_cas_dead_letter_increase_is_recorded_and_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DLQ increment in the resume CAS remains a recovery condition, not success."""

    journal = {
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "pauseRevision": 1,
            "resumeRevision": None,
            "pauseHolder": OPERATION_ID,
        }
    }
    response = {
        "_httpStatus": 200,
        **_edge_status(paused=False, revision=2),
        "dead_count": 1,
    }
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args, **_kwargs: _edge_status(paused=True, revision=1),
    )
    monkeypatch.setattr(runtime_update, "_call_internal_api", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="resume CAS"):
        runtime_update._resume_edge(
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )
    assert journal["edge"]["latestDeadCount"] == 1


def test_resume_preflight_dead_letter_increase_stops_before_resume_cas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DLQ increase observed before resume never triggers the resume mutation."""

    journal = {
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "pauseRevision": 1,
            "resumeRevision": None,
            "pauseHolder": OPERATION_ID,
        }
    }
    status = _edge_status(paused=True, revision=1) | {"dead_count": 1}
    monkeypatch.setattr(runtime_update, "_current_edge_status", lambda *_args, **_kwargs: status)
    monkeypatch.setattr(
        runtime_update,
        "_call_internal_api",
        lambda *_args, **_kwargs: pytest.fail("resume CAS must not run after a DLQ increase"),
    )

    with pytest.raises(runtime_update.RuntimeUpdateError, match="resume preflight readback"):
        runtime_update._resume_edge(
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )
    assert journal["edge"]["latestDeadCount"] == 1


def test_unobserved_initial_pause_is_replayed_instead_of_assumed_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Linearize a delayed initial pause with the same idempotent holder before rollback."""

    journal = {
        "operationId": OPERATION_ID,
        "phase": "RECOVERY_REQUIRED",
        "state": "RECOVERY_REQUIRED",
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialRevision": 0,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "pauseHolder": OPERATION_ID,
            "pauseIntent": True,
            "pauseRevision": None,
            "resumeRevision": None,
            "pauseHistory": [],
            "repauseExpectedRevision": None,
            "repauseHolder": None,
        },
    }
    spool = {"paused": False, "revision": 0, "holder": None}
    calls: list[dict[str, Any]] = []
    original_pause_is_delayed = True

    def current_edge_status(*_args: Any) -> dict[str, Any]:
        return _edge_status(paused=spool["paused"], revision=spool["revision"])

    def call_internal_api(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal original_pause_is_delayed
        body = kwargs["body"]
        calls.append(body)
        if original_pause_is_delayed:
            # The original request reaches the spool after the recovery GET but before its replay.
            spool.update(paused=True, revision=1, holder=OPERATION_ID)
            original_pause_is_delayed = False
        if spool["paused"]:
            assert body["holder"] == spool["holder"]
            assert body["expected_revision"] == spool["revision"] - 1
        else:
            assert body["expected_revision"] == spool["revision"]
            spool.update(paused=True, revision=spool["revision"] + 1, holder=body["holder"])
        return {"_httpStatus": 200, **_edge_status(paused=True, revision=spool["revision"])}

    monkeypatch.setattr(runtime_update, "_current_edge_status", current_edge_status)
    monkeypatch.setattr(runtime_update, "_call_internal_api", call_internal_api)
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)

    runtime_update._recover_edge_pause(
        journal=journal,
        journal_path=tmp_path / "operation.json",
        docker_prefix=["docker"],
    )

    assert calls == [{"holder": OPERATION_ID, "expected_revision": 0}]
    assert spool == {"paused": True, "revision": 1, "holder": OPERATION_ID}
    assert journal["edge"]["pauseRevision"] == 1


def test_candidate_drain_commit_is_recognized_after_recovery_phase_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read back the persisted generation after a drain POST commits but its reply is lost."""

    container_id = "4" * 64
    journal = {
        "operationId": OPERATION_ID,
        "phase": "RECOVERY_REQUIRED",
        "state": "RECOVERY_REQUIRED",
        "navigator": {
            "initialContainerId": OLD_CONTAINER_ID,
            "initialInstanceId": "old-instance",
            "initialGeneration": 0,
            "drainGeneration": None,
            "rollbackDrain": {
                "containerId": container_id,
                "instanceId": "candidate-instance",
                "initialGeneration": 0,
                "generation": None,
            },
        },
    }
    current_status = _native_navigator_status("drained")
    current_status["instanceId"] = "candidate-instance"
    monkeypatch.setattr(
        runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: current_status,
    )
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)
    monkeypatch.setattr(
        runtime_update,
        "_call_internal_api",
        lambda *_args, **_kwargs: pytest.fail(
            "recovery must not repeat the committed drain request"
        ),
    )

    runtime_update._drain_current_navigator_for_rollback(
        plan={"navigatorDrain": {"containerId": OLD_CONTAINER_ID}},
        journal=journal,
        journal_path=tmp_path / "operation.json",
        docker_prefix=["docker"],
        container_id=container_id,
        timeout_seconds=60,
    )

    assert journal["navigator"]["rollbackDrain"]["generation"] == 1


def test_candidate_compose_timeout_is_journaled_and_resume_never_retries_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hold the edge after timeout and do not infer a failed Compose mutation from one read."""

    active = _active_service(OLD_CONTAINER_ID, OLD_IMAGE_ID, "exchange:old")
    plan = {
        "project": "test-project",
        "targets": ["exchange"],
        "activeServices": {"exchange": active},
        "candidateImages": {
            "exchange": {
                "lockService": "exchange",
                "image": "exchange:new",
                "imageId": NEW_IMAGE_ID,
                "sourceCommits": {},
            }
        },
        "candidateInputs": {"composeFiles": [], "envFiles": []},
        "policy": {
            "dockerPrefix": ["docker"],
            "drainTimeoutSeconds": 60,
            "healthTimeoutSeconds": 30,
        },
    }
    journal = {
        "operationId": OPERATION_ID,
        "planSha256": "d" * 64,
        "phase": "EDGE_PAUSED",
        "state": "RUNNING",
        "targets": ["exchange"],
        "updatedTargets": [],
        "targetProgress": {},
        "edge": {"containerId": "3" * 64, "initialRevision": 0},
        "navigator": {},
        "history": [],
        "createdAtUtc": "2026-10-26T04:00:00+00:00",
        "updatedAtUtc": "2026-10-26T04:00:00+00:00",
    }
    monkeypatch.setattr(
        runtime_update, "_operation_service_state", lambda **_kwargs: {"exchange": active}
    )
    monkeypatch.setattr(runtime_update, "_input_paths", lambda *_args: [Path("candidate.yaml")])
    monkeypatch.setattr(runtime_update, "_verify_runtime_unmanaged", lambda *_args: None)
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **_kwargs: (_ for _ in ()).throw(
            runtime_update.RuntimeUpdateOutcomeUnknown("timeout")
        ),
    )
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)

    with pytest.raises(runtime_update.RuntimeUpdateOutcomeUnknown):
        runtime_update._update_target(
            plan=plan,
            context={},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
            target="exchange",
        )

    assert journal["state"] == "RECOVERY_REQUIRED"
    assert journal["lastErrorCode"] == "compose_update_outcome_unknown"
    assert journal["targetProgress"]["exchange"]["phase"] == "REPLACE_OUTCOME_UNKNOWN"
    assert runtime_update._has_unresolved_docker_mutation(journal)

    # Simulate a crash after the durable pre-call intent, before the timeout handler
    # could persist its unknown-outcome marker.
    journal["state"] = "RUNNING"
    journal["phase"] = "REPLACING_EXCHANGE"
    journal.pop("lastErrorCode", None)
    journal.pop("unknownDockerMutation", None)
    journal["targetProgress"]["exchange"]["phase"] = "REPLACE_REQUESTED"

    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(mode=0o700)
    lock_directory = tmp_path / "project-locks"
    lock_directory.mkdir(mode=0o700)
    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_DIRECTORY", lock_directory)
    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_OWNER_UID", os.geteuid())
    monkeypatch.setattr(runtime_update, "_read_journal", lambda *_args: journal)
    monkeypatch.setattr(runtime_update, "_plan_for_journal", lambda *_args: (plan, Path("plan")))
    events: list[str] = []
    monkeypatch.setattr(
        runtime_update,
        "_recover_edge_pause",
        lambda **_kwargs: events.append("confirm-edge-hold"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_resolve_plan_inputs",
        lambda *_args, **_kwargs: pytest.fail("unknown Docker outcome must not run preflight"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_rollback_operation",
        lambda **_kwargs: pytest.fail("unknown candidate Compose must not roll back"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **_kwargs: pytest.fail("unknown candidate Compose must not be repeated"),
    )

    with pytest.raises(runtime_update.RuntimeUpdateError, match="no replacement was retried"):
        runtime_update.resume_operation(
            OPERATION_ID,
            expected_digest="d" * 64,
            journal_dir=journal_dir,
        )

    assert events == ["confirm-edge-hold"]


def test_rollback_compose_timeout_is_not_retried_by_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Persist rollback Compose uncertainty and retain the edge hold without a second up."""

    old_exchange = _active_service(OLD_CONTAINER_ID, OLD_IMAGE_ID, "exchange:old")
    candidate_exchange = _active_service(NEW_CONTAINER_ID, NEW_IMAGE_ID, "exchange:new")
    navigator = _active_service("4" * 64, "sha256:" + "c" * 64, "navigator:v1")
    plan = {
        "project": "test-project",
        "targets": ["exchange"],
        "activeServices": {"exchange": old_exchange},
        "candidateImages": {
            "exchange": {
                "lockService": "exchange",
                "image": "exchange:new",
                "imageId": NEW_IMAGE_ID,
                "sourceCommits": {},
            }
        },
        "activeInputs": {"composeFiles": [], "envFiles": []},
        "policy": {
            "dockerPrefix": ["docker"],
            "drainTimeoutSeconds": 60,
            "healthTimeoutSeconds": 30,
        },
    }
    journal = {
        "operationId": OPERATION_ID,
        "planSha256": "e" * 64,
        "phase": "EDGE_PAUSED",
        "state": "RUNNING",
        "targets": ["exchange"],
        "updatedTargets": [],
        "targetProgress": {},
        "edge": {"containerId": "3" * 64, "initialRevision": 0},
        "navigator": {},
        "history": [],
        "createdAtUtc": "2026-10-26T04:00:00+00:00",
        "updatedAtUtc": "2026-10-26T04:00:00+00:00",
    }

    def complete_candidate(**_kwargs: Any) -> None:
        journal["updatedTargets"] = ["exchange"]
        journal["targetProgress"]["exchange"] = {
            "phase": "UPDATED_HEALTHY",
            "oldContainerId": OLD_CONTAINER_ID,
            "oldImageId": OLD_IMAGE_ID,
            "candidateImageId": NEW_IMAGE_ID,
        }

    monkeypatch.setattr(runtime_update, "_pause_edge", lambda **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_drain_navigator", lambda **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_update_target", complete_candidate)
    monkeypatch.setattr(
        runtime_update,
        "_ensure_navigator_resumed",
        lambda **_kwargs: (_ for _ in ()).throw(runtime_update.RuntimeUpdateError("late failure")),
    )
    monkeypatch.setattr(runtime_update, "_recover_edge_pause", lambda **_kwargs: None)
    monkeypatch.setattr(
        runtime_update, "_recover_old_navigator_before_candidate_start", lambda **_kwargs: False
    )
    monkeypatch.setattr(
        runtime_update,
        "_read_transition_state",
        lambda **_kwargs: {"exchange": candidate_exchange, "navigator": navigator},
    )
    monkeypatch.setattr(
        runtime_update, "_drain_current_navigator_for_rollback", lambda **_kwargs: None
    )
    monkeypatch.setattr(runtime_update, "_verify_runtime_unmanaged", lambda *_args: None)
    monkeypatch.setattr(runtime_update, "_input_paths", lambda *_args: [Path("active.yaml")])
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **_kwargs: (_ for _ in ()).throw(
            runtime_update.RuntimeUpdateOutcomeUnknown("timeout")
        ),
    )
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)

    with pytest.raises(
        runtime_update.RuntimeUpdateError, match="automatic rollback could not be verified"
    ):
        runtime_update._execute_update(
            plan={**plan, "policy": {**plan["policy"], "updateOrder": ["exchange"]}},
            context={},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )

    assert journal["state"] == "RECOVERY_REQUIRED"
    assert journal["lastErrorCode"] == "docker_rollback_outcome_unknown"
    assert journal["targetProgress"]["exchange"]["phase"] == "ROLLBACK_REQUESTED"

    # Simulate a crash after the old-image Compose intent was persisted but before
    # the timeout handler's unknown-outcome journal update.
    journal["state"] = "ROLLING_BACK"
    journal["phase"] = "ROLLBACK_EXCHANGE_REQUESTED"
    journal.pop("lastErrorCode", None)
    journal.pop("unknownDockerMutation", None)

    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(mode=0o700)
    lock_directory = tmp_path / "project-locks"
    lock_directory.mkdir(mode=0o700)
    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_DIRECTORY", lock_directory)
    monkeypatch.setattr(runtime_update, "GLOBAL_LOCK_OWNER_UID", os.geteuid())
    monkeypatch.setattr(runtime_update, "_read_journal", lambda *_args: journal)
    monkeypatch.setattr(runtime_update, "_plan_for_journal", lambda *_args: (plan, Path("plan")))
    events: list[str] = []
    monkeypatch.setattr(
        runtime_update,
        "_recover_edge_pause",
        lambda **_kwargs: events.append("confirm-edge-hold"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_resolve_plan_inputs",
        lambda *_args, **_kwargs: pytest.fail("unknown rollback must not rerender or mutate"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_rollback_operation",
        lambda **_kwargs: pytest.fail("unknown rollback must not be retried"),
    )
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **_kwargs: pytest.fail("unknown rollback Compose must not be repeated"),
    )

    with pytest.raises(runtime_update.RuntimeUpdateError, match="no replacement was retried"):
        runtime_update.resume_operation(
            OPERATION_ID,
            expected_digest="e" * 64,
            journal_dir=journal_dir,
        )

    assert events == ["confirm-edge-hold"]


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
            "stop": {
                "containerId": OLD_CONTAINER_ID,
                "commandExitCode": 0,
                "exitCode": 0,
                "graceful": True,
                "startedAt": "2026-10-26T04:00:00Z",
                "finishedAt": "2026-10-26T04:01:00Z",
            },
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
            **_native_navigator_status("accepting"),
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
    assert isinstance(journal["navigator"]["restartIntent"]["composeCommandReturnedAtUtc"], str)
    assert not runtime_update._has_unresolved_docker_mutation(journal)


def test_resume_completes_a_returned_old_image_restart_without_reissuing_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read back a durably returned old-image restart after a crash before final journaling."""

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
        "phase": "NAVIGATOR_ACTIVE_IMAGE_RESTART_REQUESTED",
        "state": "ROLLING_BACK",
        "targets": ["navigator"],
        "updatedTargets": [],
        "targetProgress": {
            "navigator": {
                "phase": "ACTIVE_IMAGE_RESTART_REQUESTED",
                "oldContainerId": OLD_CONTAINER_ID,
                "oldImageId": OLD_IMAGE_ID,
            }
        },
        "navigator": {
            "initialContainerId": OLD_CONTAINER_ID,
            "initialInstanceId": "old-instance",
            "stop": {
                "containerId": OLD_CONTAINER_ID,
                "commandExitCode": 0,
                "exitCode": 0,
                "graceful": True,
                "startedAt": "2026-10-26T04:00:00Z",
                "finishedAt": "2026-10-26T04:01:00Z",
            },
            "restartIntent": {
                "kind": "active-image-restart",
                "reason": "interrupted_before_candidate",
                "stoppedContainerId": OLD_CONTAINER_ID,
                "activeImageId": OLD_IMAGE_ID,
                "composeCommandExitCode": 0,
                "composeCommandReturnedAtUtc": "2026-10-26T04:02:00+00:00",
            },
            "processRestart": None,
        },
        "edge": {},
        "history": [],
        "updatedAtUtc": "2026-10-26T04:02:00+00:00",
    }
    restarted = _active_service(NEW_CONTAINER_ID, OLD_IMAGE_ID, "nav:old")
    monkeypatch.setattr(
        runtime_update,
        "_list_project_navigator_containers",
        lambda **_kwargs: [
            {
                "containerId": NEW_CONTAINER_ID,
                "imageRef": "nav:old",
                "imageId": OLD_IMAGE_ID,
                "running": True,
            }
        ],
    )
    monkeypatch.setattr(
        runtime_update, "_read_transition_state", lambda **_kwargs: {"navigator": restarted}
    )
    monkeypatch.setattr(
        runtime_update, "_wait_for_exact_service_health", lambda **_kwargs: restarted
    )
    monkeypatch.setattr(runtime_update, "_verify_runtime_unmanaged", lambda *_args: None)
    monkeypatch.setattr(runtime_update, "_verify_live_network_membership", lambda *_args: None)
    monkeypatch.setattr(runtime_update, "_set_phase", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_atomic_journal_write", lambda *_args: None)
    monkeypatch.setattr(
        runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            **_native_navigator_status("accepting"),
            "instanceId": "fresh-instance",
        },
    )
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **_kwargs: pytest.fail("resume must not repeat a returned Compose operation"),
    )

    assert not runtime_update._has_unresolved_docker_mutation(journal)
    assert runtime_update._recover_old_navigator_before_candidate_start(
        plan=plan,
        context={"activeCompose": {}, "activeLock": {}},
        journal=journal,
        journal_path=tmp_path / "operation.json",
        docker_prefix=["docker"],
    )
    assert journal["targetProgress"]["navigator"]["phase"] == "RECOVERY_OLD_IMAGE_RESTARTED"
    assert journal["navigator"]["processRestart"]["composeCommandExitCode"] == 0


@pytest.mark.parametrize(
    ("update_order", "targets", "expected_target_updates"),
    [
        (["exchange", "navigator"], ["exchange", "navigator"], ["exchange", "navigator"]),
        (["github-issues"], ["github-issues"], ["github-issues"]),
        (
            ["github-issues", "navigator"],
            ["github-issues", "navigator"],
            ["github-issues", "navigator"],
        ),
    ],
)
def test_successful_update_holds_edge_around_provider_only_or_combined_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    update_order: list[str],
    targets: list[str],
    expected_target_updates: list[str],
) -> None:
    """Pause ingress and drain Nav once around provider-only or combined updates."""

    plan = {"policy": {"updateOrder": update_order}}
    journal = {
        "schemaVersion": runtime_update.JOURNAL_SCHEMA,
        "operationId": OPERATION_ID,
        "planSha256": "c" * 64,
        "state": "RUNNING",
        "phase": "PRECHECKED",
        "targets": targets,
        "updatedTargets": [],
        "createdAtUtc": "2026-10-25T18:00:00+00:00",
        "updatedAtUtc": "2026-10-25T18:00:00+00:00",
        "lastErrorCode": None,
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "resumeRevision": None,
        },
        "navigator": {"stop": None},
    }
    events: list[str] = []

    def record_target_update(**kwargs: Any) -> None:
        target = kwargs["target"]
        events.append(f"update-{target}")
        journal["updatedTargets"].append(target)
        journal["updatedTargets"].sort(key=runtime_update.TARGET_ORDER.index)

    monkeypatch.setattr(
        runtime_update, "_pause_edge", lambda **_kwargs: events.append("edge-pause")
    )
    monkeypatch.setattr(
        runtime_update, "_drain_navigator", lambda **_kwargs: events.append("nav-drain")
    )
    monkeypatch.setattr(
        runtime_update,
        "_update_target",
        record_target_update,
    )
    monkeypatch.setattr(
        runtime_update,
        "_ensure_navigator_resumed",
        lambda **_kwargs: events.append("nav-ready"),
    )

    def resume_edge(**_kwargs: Any) -> None:
        journal["edge"]["resumeRevision"] = 2
        events.append("edge-resume")

    monkeypatch.setattr(runtime_update, "_resume_edge", resume_edge)
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args, **_kwargs: (
            events.append("edge-final-readback")
            or {"_httpStatus": 200, **_edge_status(paused=False, revision=2)}
        ),
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

    assert events == ["edge-pause", "nav-drain"] + [
        f"update-{target}" for target in expected_target_updates
    ] + ["nav-ready", "edge-resume", "edge-final-readback"]
    assert result["state"] == "APPLIED"
    assert result["phase"] == "UPDATE_COMPLETE"
    assert result["updatedTargets"] == update_order


def test_final_dead_letter_increment_after_resume_prevents_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A post-CAS DLQ increase is caught by final readback before APPLIED."""

    journal = {
        "schemaVersion": runtime_update.JOURNAL_SCHEMA,
        "operationId": OPERATION_ID,
        "planSha256": "c" * 64,
        "state": "RUNNING",
        "phase": "PRECHECKED",
        "targets": ["exchange"],
        "updatedTargets": [],
        "edge": {
            "containerId": OLD_CONTAINER_ID,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "resumeRevision": None,
        },
        "navigator": {},
        "history": [],
    }

    monkeypatch.setattr(runtime_update, "_pause_edge", lambda **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_drain_navigator", lambda **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_update_target", lambda **_kwargs: None)
    monkeypatch.setattr(runtime_update, "_ensure_navigator_resumed", lambda **_kwargs: None)
    monkeypatch.setattr(
        runtime_update,
        "_resume_edge",
        lambda **_kwargs: journal["edge"].update(resumeRevision=2),
    )
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args, **_kwargs: {
            "_httpStatus": 200,
            **_edge_status(paused=False, revision=2),
            "dead_count": 1,
        },
    )
    monkeypatch.setattr(
        runtime_update,
        "_set_phase",
        lambda target_journal, _path, phase, *, state=None, error_code=None: target_journal.update(
            {
                "phase": phase,
                "state": state or target_journal["state"],
                "lastErrorCode": error_code,
            }
        ),
    )
    monkeypatch.setattr(
        runtime_update,
        "_rollback_operation",
        lambda **_kwargs: (_ for _ in ()).throw(
            runtime_update.RuntimeUpdateError("DLQ count changed; preserve delivery state")
        ),
    )

    with pytest.raises(
        runtime_update.RuntimeUpdateError, match="automatic rollback could not be verified"
    ):
        runtime_update._execute_update(
            plan={"policy": {"updateOrder": ["exchange"]}},
            context={},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )

    assert journal["edge"]["latestDeadCount"] == 1
    assert journal["state"] == "RECOVERY_REQUIRED"
    assert journal["phase"] == "RECOVERY_REQUIRED"
