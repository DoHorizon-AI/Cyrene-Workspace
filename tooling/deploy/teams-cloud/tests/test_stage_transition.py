"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_stage_transition.py                                              │
│  Module: tooling.deploy.teams_cloud.tests.test_stage_transition          │
│  Role: Verify closed topology and recovery rules without Docker/provider IO.│
│                                                                          │
│  模块职责：验证拓扑与恢复约束，不访问 Docker 或 Provider。                │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
stage_transition = importlib.import_module("stage_transition")
runtime_update = importlib.import_module("runtime_update")


def _image_id(number: int) -> str:
    """Return one deterministic image ID for the synthetic locked topology."""

    return "sha256:" + f"{number:064x}"


def _service(image: str, *, profiles: list[str] | None = None) -> dict[str, Any]:
    """Build one minimal Compose service for topology-delta tests."""

    row: dict[str, Any] = {"image": image, "restart": "unless-stopped", "environment": {}}
    if profiles is not None:
        row["profiles"] = profiles
    return row


def _mount_service(image: str, *, executor: bool = False) -> dict[str, Any]:
    """Build a Navigator service that preserves the two durable mount boundaries."""

    row = _service(image)
    row["environment"]["NAVIGATOR_DATABASE_PATH"] = "/var/lib/cyrene-navigator/persistence.db"
    row["volumes"] = [
        {"type": "volume", "source": "navigator-sessions", "target": "/var/lib/cyrene-navigator"}
    ]
    if executor:
        row["volumes"].append(
            {
                "type": "bind",
                "source": "/private/codebuddy",
                "target": "/home/navigator/.codebuddy",
            }
        )
    return row


def _install_internal_status_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    edge_status: dict[str, Any] | None = None,
    navigator_status: dict[str, Any] | None = None,
) -> list[str]:
    """Return genuine HTTP 200 envelopes through the internal status transport layer."""

    edge_document = edge_status or {
        "paused": False,
        "revision": 7,
        "active_delivery_count": 0,
        "queued_count": 0,
        "leased_count": 0,
        "retry_wait_count": 0,
        "delivered_count": 1,
        "rejected_count": 0,
        "dead_count": 0,
        "pending_bytes": 0,
        "retained_body_bytes": 0,
        "oldest_pending_at": None,
        "last_delivered_at": None,
    }
    if navigator_status is None:
        fixture_path = (
            Path(__file__).resolve().parent / "fixtures/navigator-drain-status-18baefba.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        navigator_status = fixture["statuses"]["accepting"]
    requested_urls: list[str] = []

    def run_command(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        url = next(
            (value for value in argv if isinstance(value, str) and value.startswith("http://")),
            None,
        )
        if not isinstance(url, str):
            raise TypeError("internal status request omitted its URL")
        requested_urls.append(url)
        if url.endswith("/_internal/teams/delivery/status"):
            document = edge_document
        elif url.endswith("/internal/v1/executor/drain"):
            document = navigator_status
        else:
            raise AssertionError(f"unexpected internal status endpoint: {url}")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"httpStatus": 200, "document": document}),
        )

    monkeypatch.setattr(
        runtime_update,
        "_internal_control_probe_target",
        lambda _prefix, container_id, **_kwargs: (
            container_id,
            "sha256:" + "a" * 64,
            "/private/test-only-token",
        ),
    )
    monkeypatch.setattr(runtime_update, "_run_command", run_command)
    return requested_urls


def test_create_plan_consumes_statuses_after_transport_and_single_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the planner through raw HTTP 200 envelopes, not mocked status helpers."""

    edge_container_id = "1" * 64
    navigator_container_id = "2" * 64
    requested_urls = _install_internal_status_transport(monkeypatch)
    active_services = {
        "teams-ingress": {
            "containerId": edge_container_id,
            "imageRef": "teams-ingress:test",
            "imageId": _image_id(1),
        },
        "navigator": {
            "containerId": navigator_container_id,
            "imageRef": "navigator:test",
            "imageId": _image_id(2),
        },
    }
    compose = {
        "name": "cyrene-teams-cloud",
        "services": {"teams-ingress": {}, "navigator": {}},
        "secrets": {},
        "configs": {},
        "volumes": {},
    }
    runtime_services = {"teams-ingress": {}, "navigator": {}}
    mount_identity = {"teams-ingress": [], "navigator": []}
    candidate_preflight = {
        "mode": "connector-auth-readonly",
        "connectorAuthPhase": "provider-bootstrap",
        "credentialReadOnlyRuntimeReady": True,
        "fullCloudWriteDeploymentReady": False,
        "lifecycleAcceptance": "NOT_RUN",
        "expectedProviderScopeDigests": {},
        "huggingfaceRepositoryScopeDigestSha256": "a" * 64,
    }
    seed_file = tmp_path / "huggingface-oauth.json"
    evidence_file = tmp_path / "connector-auth-evidence.json"
    seed_file.write_text("{}", encoding="utf-8")
    evidence_file.write_text("{}", encoding="utf-8")
    persisted_plan: dict[str, Any] = {}

    monkeypatch.setattr(runtime_update, "_require_update_operator", lambda: None)
    monkeypatch.setattr(runtime_update, "_validate_docker_prefix", lambda _prefix: None)
    monkeypatch.setattr(runtime_update, "_load_lock", lambda _path: {})

    def capture_plan(_path: Path, document: dict[str, Any], **_kwargs: Any) -> None:
        persisted_plan.update(document)

    monkeypatch.setattr(runtime_update, "_write_private_json", capture_plan)
    monkeypatch.setattr(stage_transition, "_validate_output_directory", lambda _path: None)
    monkeypatch.setattr(
        stage_transition,
        "_validate_private_directory",
        lambda path, *_args, **_kwargs: Path(path),
    )
    monkeypatch.setattr(
        stage_transition,
        "_file_records",
        lambda paths, _label: [
            {"path": str(Path(path).resolve()), "sha256": "b" * 64} for path in paths
        ],
    )
    monkeypatch.setattr(
        stage_transition,
        "_file_record",
        lambda path, _label: {"path": str(Path(path).resolve()), "sha256": "c" * 64},
    )
    monkeypatch.setattr(stage_transition, "_directory_manifest", lambda *_args: [])
    monkeypatch.setattr(stage_transition.preflight, "_compose_config", lambda *_args: compose)
    monkeypatch.setattr(stage_transition, "_secret_manifest", lambda *_args: [])
    monkeypatch.setattr(stage_transition, "_validate_active_candidate_configs", lambda *_args: None)
    monkeypatch.setattr(stage_transition, "_validate_active_candidate_secrets", lambda *_args: None)
    monkeypatch.setattr(
        stage_transition, "_validate_candidate_secret_paths", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        stage_transition,
        "_validate_phase_topology",
        lambda **_kwargs: (["navigator"], {"navigator": {"added": False}}),
    )
    monkeypatch.setattr(
        stage_transition.preflight, "run_preflight", lambda **_kwargs: candidate_preflight
    )
    monkeypatch.setattr(
        stage_transition,
        "_selection_file_state",
        lambda path, *_args, **_kwargs: {
            "path": str(Path(path).resolve()),
            "sha256": None,
            "document": None,
        },
    )
    monkeypatch.setattr(
        stage_transition, "_runtime_service_inventory", lambda _compose: runtime_services
    )
    monkeypatch.setattr(
        stage_transition,
        "_runtime_config_identity_for_compose",
        lambda _compose: mount_identity,
    )
    monkeypatch.setattr(
        stage_transition,
        "_candidate_runtime_config_identity",
        lambda **_kwargs: mount_identity,
    )
    monkeypatch.setattr(
        stage_transition, "_verify_runtime_config_identity_sources", lambda *_args: None
    )
    monkeypatch.setattr(
        stage_transition, "_live_service_inventory", lambda **_kwargs: active_services
    )
    monkeypatch.setattr(stage_transition, "_validate_plan", lambda _plan: None)

    compose_file = tmp_path / "compose.yaml"
    env_file = tmp_path / "runtime.env"
    lock_file = tmp_path / "image-lock.json"
    result = stage_transition.create_plan(
        phase="provider-bootstrap",
        active_compose_files=[compose_file],
        active_env_files=[env_file],
        active_image_lock=lock_file,
        candidate_compose_files=[compose_file],
        candidate_env_files=[env_file],
        candidate_image_lock=lock_file,
        secrets_dir=tmp_path / "secrets",
        active_config_dir=tmp_path / "active-config",
        candidate_config_dir=tmp_path / "candidate-config",
        candidate_huggingface_seed=seed_file,
        candidate_runpod_key_file=None,
        connector_auth_evidence=evidence_file,
        connector_auth_task_id="teams-status-test",
        notification_anchor_file=None,
        notification_provenance_file=None,
        active_selection_file=None,
        promote_selection_file=tmp_path / "runtime-selection.json",
        backup_dir=tmp_path,
        journal_dir=tmp_path,
        plan_path=tmp_path / "plan.json",
        docker_prefix=["docker"],
        drain_timeout_seconds=300,
        health_timeout_seconds=300,
        max_memory_mib=3584,
        max_cpus=1.95,
        required_free_gib=2.0,
        minimum_mem_available_mib=1024,
    )

    assert len(result["planSha256"]) == 64
    assert "imageUpdateTarget" not in persisted_plan
    assert persisted_plan["edgeDelivery"]["revision"] == 7
    assert persisted_plan["navigatorDrain"]["instanceId"] == "11111111-1111-4111-8111-111111111111"
    assert len(requested_urls) == 2
    assert any("/_internal/teams/delivery/status" in url for url in requested_urls)
    assert any("/internal/v1/executor/drain" in url for url in requested_urls)


def test_apply_baseline_consumes_statuses_after_transport_and_single_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise apply's active baseline through the raw 200 status envelopes."""

    edge_container_id = "3" * 64
    navigator_container_id = "4" * 64
    requested_urls = _install_internal_status_transport(monkeypatch)
    active_services = {
        "teams-ingress": {
            "containerId": edge_container_id,
            "imageRef": "teams-ingress:test",
            "imageId": _image_id(3),
        },
        "navigator": {
            "containerId": navigator_container_id,
            "imageRef": "navigator:test",
            "imageId": _image_id(4),
        },
    }
    compose = {"name": "cyrene-teams-cloud", "services": {}}
    monkeypatch.setattr(runtime_update, "_load_lock", lambda _path: {})
    monkeypatch.setattr(stage_transition.preflight, "_compose_config", lambda *_args: compose)
    monkeypatch.setattr(
        stage_transition,
        "_live_service_inventory",
        lambda **_kwargs: active_services,
    )
    plan = {
        "activeInputs": {
            "composeFiles": [{"path": "/private/active-compose.yaml"}],
            "envFiles": [{"path": "/private/active.env"}],
            "imageLock": {"path": "/private/active-image-lock.json"},
        },
        "project": "cyrene-teams-cloud",
        "runtimeConfigIdentity": {"active": {"teams-ingress": [], "navigator": []}},
        "activeServices": active_services,
        "edgeDelivery": {
            "containerId": edge_container_id,
            "revision": 7,
            "deadCount": 0,
        },
        "navigatorDrain": {
            "containerId": navigator_container_id,
            "instanceId": "11111111-1111-4111-8111-111111111111",
            "generation": 0,
        },
    }

    active_compose, _lock_data = stage_transition._verify_active_baseline(plan, ["docker"])

    assert active_compose == compose
    assert len(requested_urls) == 2


def test_apply_preflight_rechecks_frozen_host_fronted_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignore only the new check timestamp and block changed host Caddy/CA state."""

    planned_host = {
        "schemaVersion": "cyrene-host-fronted-ingress-readiness-v1",
        "status": "HOST_FRONTED_STAGING_READY",
        "ingressExposure": "host-fronted-staging",
        "accepted": False,
        "deploymentReady": False,
        "hostname": "bot.example.test",
        "ingressImageId": _image_id(1),
        "activeCaddyfileSha256": "a" * 64,
        "caddyBinarySha256": "b" * 64,
        "backendCaSha256": "c" * 64,
        "backendTls": "PASS",
        "backendUnsignedCallbackHttpStatus": 401,
        "publicTls": "PASS",
        "publicUnsignedCallbackHttpStatus": 401,
        "publicCallbackPostApply": "NOT_RUN",
        "fullTeamsAcceptance": "NOT_RUN",
        "checkedAtUtc": "2026-10-26T12:00:00+00:00",
    }
    fresh_host = {**planned_host, "checkedAtUtc": "2026-10-26T12:01:00+00:00"}
    result = {
        "credentialReadOnlyRuntimeReady": True,
        "fullCloudWriteDeploymentReady": False,
        "lifecycleAcceptance": "NOT_RUN",
        "expectedProviderScopeDigests": {},
        "huggingfaceRepositoryScopeDigestSha256": "d" * 64,
        "hostFrontedIngressReadiness": fresh_host,
    }
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        stage_transition.preflight,
        "run_preflight",
        lambda **kwargs: captured.update(kwargs) or result,
    )
    plan = {
        "phase": "provider-bootstrap",
        "candidateInputPaths": {"connectorAuthTaskId": "task-123"},
        "candidateInputs": {
            "connectorAuthEvidence": {"path": "/private/evidence.json"},
            "huggingfaceSeed": {"path": "/private/hf-seed.json"},
            "composeFiles": [{"path": "/private/compose.yaml"}],
            "envFiles": [{"path": "/private/runtime.env"}],
            "imageLock": {"path": "/private/image-lock.json"},
        },
        "activeSecretsDir": "/private/secrets",
        "candidateConfigDir": "/private/config",
        "policy": {"dockerPrefix": ["docker"]},
        "candidatePreflight": {
            "ingressExposure": "host-fronted-staging",
            "hostCaddyBinary": "/usr/local/bin/caddy",
            "opensslBinary": "openssl",
            "hostFrontedIngressReadiness": planned_host,
            "expectedProviderScopeDigests": {},
            "huggingfaceRepositoryScopeDigestSha256": "d" * 64,
            "notificationInputs": {},
            "maxMemoryMiB": 3584,
            "maxCpus": 1.95,
            "requiredFreeGiB": 2.0,
            "minimumMemAvailableMiB": 1024,
        },
    }

    assert stage_transition._candidate_preflight(plan) == result
    assert captured["ingress_exposure"] == "host-fronted-staging"
    assert captured["host_caddy_binary"] == "/usr/local/bin/caddy"

    result["hostFrontedIngressReadiness"] = {**fresh_host, "backendCaSha256": "e" * 64}
    with pytest.raises(stage_transition.StageTransitionError, match="live host-fronted readiness"):
        stage_transition._candidate_preflight(plan)


def test_post_update_capacity_maps_overlapping_service_names_by_container_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use exact IDs when service names overlap, regardless of Docker output order."""

    container_ids = {
        "navigator": "1" * 64,
        "navigator-persistence": "2" * 64,
        "computer-runtime": "3" * 64,
        "computer-runtime-proxy": "4" * 64,
    }
    services = {name: {"containerId": container_id} for name, container_id in container_ids.items()}
    stats_rows = [
        (container_ids["computer-runtime-proxy"], "400MiB / 512MiB"),
        (container_ids["navigator-persistence"], "200MiB / 512MiB"),
        (container_ids["computer-runtime"], "300MiB / 512MiB"),
        (container_ids["navigator"], "100MiB / 512MiB"),
    ]
    captured: dict[str, Any] = {}

    monkeypatch.setattr(stage_transition.preflight, "_parse_memavailable", lambda: 2048)
    monkeypatch.setattr(stage_transition.preflight, "_parse_meminfo", lambda: 8192)

    def checked_stdout(argv: list[str], **kwargs: Any) -> str:
        captured["argv"] = argv
        captured.update(kwargs)
        return "\n".join(f"{container_id}|{usage}" for container_id, usage in stats_rows)

    monkeypatch.setattr(runtime_update, "_checked_stdout", checked_stdout)
    result = stage_transition._validate_post_update_capacity(
        compose={"name": "cyrene"},
        services=services,
        docker_prefix=["docker"],
        minimum_available_mib=1024,
    )

    assert result["rssByServiceMiB"] == {
        "computer-runtime": 300.0,
        "computer-runtime-proxy": 400.0,
        "navigator": 100.0,
        "navigator-persistence": 200.0,
    }
    assert result["totalRssMiB"] == 1000.0
    assert captured["argv"] == [
        "docker",
        "stats",
        "--no-stream",
        "--no-trunc",
        "--format",
        "{{.ID}}|{{.MemUsage}}",
        *sorted(container_ids.values()),
    ]
    assert captured["timeout_seconds"] == 30


@pytest.mark.parametrize(
    ("stats_rows", "message"),
    [
        (
            [("1" * 64, "100MiB / 512MiB"), ("1" * 64, "100MiB / 512MiB")],
            "duplicate RSS container ID",
        ),
        ([("9" * 64, "100MiB / 512MiB")], "unknown service container"),
        ([("1" * 63, "100MiB / 512MiB")], "malformed RSS container ID"),
        ([("1" * 64, "100MiB / 512MiB")], "complete per-service RSS"),
    ],
)
def test_post_update_capacity_rejects_invalid_rss_container_ids(
    monkeypatch: pytest.MonkeyPatch,
    stats_rows: list[tuple[str, str]],
    message: str,
) -> None:
    """Reject malformed, duplicate, unknown, or missing per-container RSS rows."""

    services = {
        "navigator": {"containerId": "1" * 64},
        "navigator-persistence": {"containerId": "2" * 64},
    }
    monkeypatch.setattr(stage_transition.preflight, "_parse_memavailable", lambda: 2048)
    monkeypatch.setattr(stage_transition.preflight, "_parse_meminfo", lambda: 8192)
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: "\n".join(
            f"{container_id}|{usage}" for container_id, usage in stats_rows
        ),
    )

    with pytest.raises(stage_transition.StageTransitionError, match=message):
        stage_transition._validate_post_update_capacity(
            compose={"name": "cyrene"},
            services=services,
            docker_prefix=["docker"],
            minimum_available_mib=1024,
        )


def test_post_update_capacity_rejects_duplicate_expected_container_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require the sealed runtime inventory to map one ID to one service."""

    monkeypatch.setattr(stage_transition.preflight, "_parse_memavailable", lambda: 2048)
    monkeypatch.setattr(stage_transition.preflight, "_parse_meminfo", lambda: 8192)
    monkeypatch.setattr(runtime_update, "_checked_stdout", lambda *_args, **_kwargs: "")

    with pytest.raises(
        stage_transition.StageTransitionError, match="services share a container ID"
    ):
        stage_transition._validate_post_update_capacity(
            compose={"name": "cyrene"},
            services={
                "navigator": {"containerId": "1" * 64},
                "navigator-persistence": {"containerId": "1" * 64},
            },
            docker_prefix=["docker"],
            minimum_available_mib=1024,
        )


def _lock(rows: list[tuple[str, str, int]]) -> dict[str, Any]:
    """Build one synthetic image lock with immutable service IDs."""

    return {
        "schemaVersion": "cyrene-teams-cloud-image-lock-v1",
        "images": [
            {
                "service": service,
                "image": image,
                "imageId": _image_id(number),
                "sourceCommits": {},
            }
            for service, image, number in rows
        ],
        "externalImages": {},
    }


def _active_and_bootstrap() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    """Return a 12-service active shape and the allowed 14-service provider candidate."""

    active_services = {
        "navigator-persistence": _mount_service("persistence:v1"),
        "navigator": _mount_service("navigator:v1", executor=True),
        "account-center": _service("account-center:v1"),
        "exchange": _service("exchange:v1"),
        "teams-ingress": _service("teams-ingress:v1"),
        "teams-public-ingress": _service("caddy:v1", profiles=["public-ingress"]),
    }
    candidate_services = {name: copy.deepcopy(service) for name, service in active_services.items()}
    candidate_services["navigator-persistence"]["image"] = "persistence:v2"
    candidate_services["account-center"]["environment"]["HF_ENABLED"] = "true"
    candidate_services["exchange"]["environment"]["PROVIDER_PHASE"] = "readonly"
    candidate_services["navigator"]["environment"]["CONNECTOR_PHASE"] = "provider-bootstrap"
    candidate_services["huggingface"] = _service("huggingface:v1")
    candidate_services["cloud-management-phase2"] = _service("cloud:v1", profiles=["cloud-phase2"])
    active = {
        "name": "cyrene-teams-cloud",
        "services": active_services,
        "networks": {},
        "volumes": {},
        "configs": {},
        "secrets": {},
    }
    candidate = {
        "name": "cyrene-teams-cloud",
        "services": candidate_services,
        "networks": {},
        "volumes": {},
        "configs": {},
        "secrets": {},
    }
    active_lock = _lock(
        [
            ("navigator-persistence", "persistence:v1", 1),
            ("dsh-executor", "navigator:v1", 2),
            ("account-center", "account-center:v1", 3),
            ("exchange", "exchange:v1", 4),
            ("teams-ingress", "teams-ingress:v1", 5),
            ("teams-public-ingress", "caddy:v1", 6),
        ]
    )
    candidate_lock = _lock(
        [
            ("navigator-persistence", "persistence:v2", 7),
            ("dsh-executor", "navigator:v1", 2),
            ("account-center", "account-center:v1", 3),
            ("exchange", "exchange:v1", 4),
            ("teams-ingress", "teams-ingress:v1", 5),
            ("teams-public-ingress", "caddy:v1", 6),
            ("huggingface-provider", "huggingface:v1", 8),
            ("cloud-management", "cloud:v1", 9),
        ]
    )
    return active, candidate, active_lock, candidate_lock


def _active_image_update(
    target: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build a fixed 14-service selector state with one candidate image changed."""

    _bootstrap_active, active, _bootstrap_lock, active_lock = _active_and_bootstrap()
    active = copy.deepcopy(active)
    active_lock = copy.deepcopy(active_lock)
    candidate = copy.deepcopy(active)
    candidate_lock = copy.deepcopy(active_lock)
    lock_service = runtime_update.LOCK_SERVICE_FOR_TARGET[target]
    candidate_ref = f"{target}:image-update"
    candidate["services"][target]["image"] = candidate_ref
    row = next(item for item in candidate_lock["images"] if item["service"] == lock_service)
    row["image"] = candidate_ref
    row["imageId"] = _image_id(50 if target == "exchange" else 51)
    return active, candidate, active_lock, candidate_lock


@pytest.mark.parametrize("target", ["exchange", "navigator"])
def test_image_update_targets_exactly_one_service_and_keeps_existing_phase(
    target: str,
) -> None:
    """Update one built image while preserving every service/configuration in the phase."""

    active, candidate, active_lock, candidate_lock = _active_image_update(target)

    targets, images = stage_transition._validate_phase_topology(
        phase="issued-connector-activation",
        image_update_target=target,
        active=active,
        candidate=candidate,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )

    assert targets == [target]
    assert images[target]["active"]["imageId"] != images[target]["candidate"]["imageId"]
    assert all(row["configChanged"] is False and row["added"] is False for row in images.values())


def test_image_update_rejects_config_and_non_target_image_changes() -> None:
    """Keep image-update plans closed to one image reference and ID change."""

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange")
    candidate["services"]["exchange"]["environment"]["NEW_ROLE"] = "unexpected"
    with pytest.raises(
        stage_transition.StageTransitionError, match="only its selected service image"
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            image_update_target="exchange",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange")
    candidate["services"]["navigator"]["image"] = "navigator:unexpected"
    candidate_lock["images"] = [
        {**row, "image": "navigator:unexpected", "imageId": _image_id(52)}
        if row["service"] == "dsh-executor"
        else row
        for row in candidate_lock["images"]
    ]
    with pytest.raises(
        stage_transition.StageTransitionError, match="only its selected service image"
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            image_update_target="exchange",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange")
    candidate_lock["images"] = [
        {**row, "imageId": _image_id(52)} if row["service"] == "dsh-executor" else row
        for row in candidate_lock["images"]
    ]
    with pytest.raises(
        stage_transition.StageTransitionError, match="only the selected built service"
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            image_update_target="exchange",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange")
    candidate_lock["externalImages"]["unexpected"] = {"imageId": _image_id(53)}
    with pytest.raises(
        stage_transition.StageTransitionError, match="only the selected built service"
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            image_update_target="exchange",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange")
    candidate["services"]["unplanned-worker"] = _service("worker:v1")
    with pytest.raises(
        stage_transition.StageTransitionError, match="exact active service inventory"
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            image_update_target="exchange",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def test_image_update_rejects_unchanged_or_unknown_targets() -> None:
    """Require one real Exchange/Navigator image change and reject other targets."""

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange")
    candidate["services"]["exchange"]["image"] = active["services"]["exchange"]["image"]
    candidate_lock["images"] = copy.deepcopy(active_lock["images"])
    with pytest.raises(stage_transition.StageTransitionError, match="distinct target reference"):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            image_update_target="exchange",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange")
    with pytest.raises(
        stage_transition.StageTransitionError, match="must be Exchange or Navigator"
    ):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            image_update_target="account-center",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def test_stage_transition_cli_closes_image_update_target_choices() -> None:
    """Expose only the two existing services whose image updates have explicit V2 gates."""

    parser = stage_transition._build_parser()
    commands = next(action for action in parser._actions if action.dest == "command")
    plan_parser = commands.choices["plan"]
    image_target_action = next(
        action
        for action in plan_parser._actions
        if "--image-update-target" in action.option_strings
    )

    assert image_target_action.choices == ["exchange", "navigator"]


def test_stage_transition_cli_passes_image_update_target_to_plan_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Forward the explicit CLI target without changing the selected config phase."""

    captured: dict[str, Any] = {}
    monkeypatch.setattr(runtime_update, "_validate_docker_prefix", lambda _prefix: None)

    def capture_plan(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"operationId": "operation-1", "planSha256": "a" * 64}

    monkeypatch.setattr(stage_transition, "create_plan", capture_plan)
    arguments = [
        "plan",
        "--phase",
        "issued-connector-activation",
        "--image-update-target",
        "exchange",
        "--active-compose-file",
        str(tmp_path / "active-compose.yaml"),
        "--active-env-file",
        str(tmp_path / "active.env"),
        "--active-image-lock",
        str(tmp_path / "active-lock.json"),
        "--candidate-compose-file",
        str(tmp_path / "candidate-compose.yaml"),
        "--candidate-env-file",
        str(tmp_path / "candidate.env"),
        "--candidate-image-lock",
        str(tmp_path / "candidate-lock.json"),
        "--secrets-dir",
        str(tmp_path / "secrets"),
        "--active-config-dir",
        str(tmp_path / "config"),
        "--candidate-config-dir",
        str(tmp_path / "config"),
        "--candidate-huggingface-seed",
        str(tmp_path / "hf-seed.json"),
        "--connector-auth-evidence",
        str(tmp_path / "connector-evidence.json"),
        "--connector-auth-task-id",
        "task-1",
        "--active-selection",
        str(tmp_path / "runtime-selection.json"),
        "--promote-selection",
        str(tmp_path / "runtime-selection.json"),
        "--backup-dir",
        str(tmp_path / "backups"),
        "--journal-dir",
        str(tmp_path / "journals"),
        "--output",
        str(tmp_path / "plan.json"),
    ]

    assert stage_transition.main(arguments) == 0
    assert captured["phase"] == "issued-connector-activation"
    assert captured["image_update_target"] == "exchange"


def _image_update_selection_inputs() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    """Build matching selector/config records for an image-only transition test."""

    compose_files = [{"path": "/private/compose.yaml", "sha256": "a" * 64}]
    env_files = [{"path": "/private/runtime.env", "sha256": "b" * 64}]
    active_lock = {"path": "/private/active-image-lock.json", "sha256": "c" * 64}
    candidate_lock = {"path": "/private/candidate-image-lock.json", "sha256": "d" * 64}
    config_files = [{"path": "principal.json", "sha256": "e" * 64}]
    secret_files = [{"name": "exchange-key", "sha256": "f" * 64}]
    seed = {"path": "/private/hf-seed.json", "sha256": "1" * 64}
    runpod_key = {"path": "/private/runpod-key", "sha256": "2" * 64}
    identity = {"navigator": []}
    active_inputs = {
        "composeFiles": compose_files,
        "envFiles": env_files,
        "imageLock": active_lock,
        "configDirectoryFiles": config_files,
        "secretFiles": secret_files,
    }
    candidate_inputs = {
        "composeFiles": compose_files,
        "envFiles": env_files,
        "imageLock": candidate_lock,
        "configDirectoryFiles": config_files,
        "secretFiles": secret_files,
        "huggingfaceSeed": seed,
        "runpodKey": runpod_key,
    }
    selection = {
        "schemaVersion": stage_transition.SELECTION_SCHEMA,
        "project": "cyrene-teams-cloud",
        "operationId": "00000000-0000-4000-8000-000000000001",
        "planSha256": "3" * 64,
        "phase": "issued-connector-activation",
        "composeFiles": compose_files,
        "envFiles": env_files,
        "imageLock": active_lock,
        "secretsDir": "/private/secrets",
        "configDir": "/private/config",
        "huggingfaceSeed": seed,
        "runpodKey": runpod_key,
        "runtimeConfigIdentity": identity,
        "updatedAtUtc": "2026-10-26T12:00:00+00:00",
    }
    selection_record = {
        "path": "/private/runtime-selection.json",
        "sha256": "4" * 64,
        "document": selection,
    }
    runtime_identity = {"active": identity, "candidate": copy.deepcopy(identity)}
    return active_inputs, candidate_inputs, selection_record, runtime_identity


def test_image_update_requires_current_selector_phase_and_unchanged_mounts() -> None:
    """Bind the image-only plan to the current phase, seed, directories, and mount map."""

    active_inputs, candidate_inputs, selection, identity = _image_update_selection_inputs()
    stage_transition._validate_image_update_selection_contract(
        target="exchange",
        phase="issued-connector-activation",
        project="cyrene-teams-cloud",
        selection_record=selection,
        selection_path="/private/runtime-selection.json",
        active_config_dir="/private/config",
        candidate_config_dir="/private/config",
        active_secrets_dir="/private/secrets",
        active_inputs=active_inputs,
        candidate_inputs=candidate_inputs,
        runtime_config_identity=identity,
    )

    with pytest.raises(
        stage_transition.StageTransitionError, match="match an existing runtime selector"
    ):
        stage_transition._validate_image_update_selection_contract(
            target="exchange",
            phase="provider-bootstrap",
            project="cyrene-teams-cloud",
            selection_record=selection,
            selection_path="/private/runtime-selection.json",
            active_config_dir="/private/config",
            candidate_config_dir="/private/config",
            active_secrets_dir="/private/secrets",
            active_inputs=active_inputs,
            candidate_inputs=candidate_inputs,
            runtime_config_identity=identity,
        )

    changed_identity = copy.deepcopy(identity)
    changed_identity["candidate"]["navigator"].append({"type": "bind"})
    with pytest.raises(
        stage_transition.StageTransitionError, match="preserve every service mount identity"
    ):
        stage_transition._validate_image_update_selection_contract(
            target="exchange",
            phase="issued-connector-activation",
            project="cyrene-teams-cloud",
            selection_record=selection,
            selection_path="/private/runtime-selection.json",
            active_config_dir="/private/config",
            candidate_config_dir="/private/config",
            active_secrets_dir="/private/secrets",
            active_inputs=active_inputs,
            candidate_inputs=candidate_inputs,
            runtime_config_identity=changed_identity,
        )

    with pytest.raises(stage_transition.StageTransitionError, match="existing runtime selector v2"):
        stage_transition._validate_image_update_selection_contract(
            target="exchange",
            phase="issued-connector-activation",
            project="cyrene-teams-cloud",
            selection_record=None,
            selection_path="/private/runtime-selection.json",
            active_config_dir="/private/config",
            candidate_config_dir="/private/config",
            active_secrets_dir="/private/secrets",
            active_inputs=active_inputs,
            candidate_inputs=candidate_inputs,
            runtime_config_identity=identity,
        )

    with pytest.raises(
        stage_transition.StageTransitionError, match="config and secret directories"
    ):
        stage_transition._validate_image_update_selection_contract(
            target="exchange",
            phase="issued-connector-activation",
            project="cyrene-teams-cloud",
            selection_record=selection,
            selection_path="/private/runtime-selection.json",
            active_config_dir="/private/config",
            candidate_config_dir="/private/candidate-config",
            active_secrets_dir="/private/secrets",
            active_inputs=active_inputs,
            candidate_inputs=candidate_inputs,
            runtime_config_identity=identity,
        )

    changed_seed_inputs = copy.deepcopy(candidate_inputs)
    changed_seed_inputs["huggingfaceSeed"]["path"] = "/private/other-hf-seed.json"
    with pytest.raises(stage_transition.StageTransitionError, match="provider seed and key"):
        stage_transition._validate_image_update_selection_contract(
            target="exchange",
            phase="issued-connector-activation",
            project="cyrene-teams-cloud",
            selection_record=selection,
            selection_path="/private/runtime-selection.json",
            active_config_dir="/private/config",
            candidate_config_dir="/private/config",
            active_secrets_dir="/private/secrets",
            active_inputs=active_inputs,
            candidate_inputs=changed_seed_inputs,
            runtime_config_identity=identity,
        )


@pytest.mark.parametrize(
    ("target", "expected"),
    [("exchange", "old"), ("navigator", "fresh"), (None, "fresh")],
)
def test_image_update_resumes_the_existing_executor_only_for_exchange(
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
    expected: str,
) -> None:
    """Exchange-only updates resume the old generation; Navigator updates keep the fresh gate."""

    observed: list[str] = []
    monkeypatch.setattr(
        stage_transition,
        "_resume_old_executor_if_needed",
        lambda **_kwargs: observed.append("old"),
    )
    monkeypatch.setattr(
        stage_transition,
        "_resume_live_navigator",
        lambda **_kwargs: observed.append("fresh"),
    )

    stage_transition._resume_executor_after_transition(
        plan={"imageUpdateTarget": target} if target is not None else {},
        journal={},
        candidate_compose={},
        docker_prefix=["docker"],
    )

    assert observed == [expected]


@pytest.mark.parametrize(
    ("phase", "target", "required"),
    [
        ("provider-bootstrap", "exchange", True),
        ("provider-bootstrap", "navigator", False),
        ("issued-connector-activation", "exchange", True),
        ("issued-connector-activation", "navigator", True),
        ("provider-bootstrap", None, False),
        ("issued-connector-activation", None, True),
    ],
)
def test_exchange_readiness_gate_tracks_phase_and_image_target(
    phase: str,
    target: str | None,
    required: bool,
) -> None:
    """Bootstrap image updates verify only their frozen catalog; activation retains full readiness."""

    assert stage_transition._exchange_readiness_required(phase, target) is required


def test_exchange_image_update_records_readiness_from_the_candidate_container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify the post-update readiness probe targets the newly started Exchange image."""

    candidate_id = "a" * 64
    candidate_image_id = _image_id(60)
    plan = {
        "candidateServices": {"exchange": {"candidate": {"imageId": candidate_image_id}}},
        "activeServices": {"exchange": {"containerId": "b" * 64, "imageId": _image_id(4)}},
    }
    journal = {
        "targetProgress": {
            "exchange": {
                "candidateContainerId": candidate_id,
                "candidateImageId": candidate_image_id,
            }
        },
        "exchangeReadiness": {"verified": False, "receipt": None},
    }
    captured: dict[str, Any] = {}

    def read_readiness(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"exchangeContainerId": kwargs["container_id"]}

    monkeypatch.setattr(stage_transition, "_read_live_exchange_readiness", read_readiness)
    monkeypatch.setattr(stage_transition, "_write_journal", lambda *_args: None)
    stage_transition._record_exchange_readiness(
        plan=plan,
        compose={"name": "cyrene"},
        journal=journal,
        journal_path=tmp_path / "journal.json",
        docker_prefix=["docker"],
    )

    assert captured["container_id"] == candidate_id
    assert captured["image_id"] == candidate_image_id
    assert journal["exchangeReadiness"]["verified"] is True
    assert journal["exchangeReadiness"]["receipt"]["exchangeContainerId"] == candidate_id


def test_cloud_management_tls_secret_overlay_allows_only_the_canonical_four_names() -> None:
    """Permit the four phase-two mTLS files while rejecting any other new secret."""

    expected = frozenset(
        {
            "cloud-management-server-cert",
            "cloud-management-server-key",
            "cloud-management-health-client-cert",
            "cloud-management-health-client-key",
        }
    )
    assert stage_transition.CLOUD_MANAGEMENT_TLS_SECRET_NAMES == expected

    active = {"secrets": {}}
    candidate = {"secrets": {name: {"file": f"/candidate/{name}"} for name in expected}}
    candidate["services"] = {
        "cloud-management-phase2": {
            "secrets": [{"source": name, "target": name} for name in sorted(expected)]
        }
    }
    stage_transition._validate_active_candidate_secrets(active, candidate)

    candidate["secrets"]["cloud-management-unexpected-secret"] = {
        "file": "/candidate/cloud-management-unexpected-secret"
    }
    with pytest.raises(
        stage_transition.StageTransitionError, match="outside the HF/cloud provider set"
    ):
        stage_transition._validate_active_candidate_secrets(active, candidate)

    del candidate["secrets"]["cloud-management-unexpected-secret"]
    candidate["services"]["exchange"] = {
        "secrets": [
            {"source": "cloud-management-server-key", "target": "cloud-management-server-key"}
        ]
    }
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="only to their canonical Cloud Management targets",
    ):
        stage_transition._validate_active_candidate_secrets(active, candidate)


def test_candidate_secrets_cannot_repoint_existing_host_secret_sources(tmp_path: Path) -> None:
    """Keep existing secret names pinned to their original host files."""

    active_dir = tmp_path / "active"
    candidate_dir = tmp_path / "candidate"
    active_dir.mkdir()
    candidate_dir.mkdir()
    active_source = active_dir / "exchange-tls-cert"
    candidate_source = candidate_dir / "exchange-tls-cert"
    active_source.write_text("active-placeholder")
    candidate_source.write_text("candidate-placeholder")
    cloud_secret_sources = {}
    cloud_mounts = []
    for name in sorted(stage_transition.CLOUD_MANAGEMENT_TLS_SECRET_NAMES):
        source = active_dir / name
        source.write_text("active-placeholder")
        cloud_secret_sources[name] = {"file": str(source)}
        cloud_mounts.append({"source": name, "target": name})
    active = {
        "secrets": {"exchange-tls-cert": {"file": str(active_source)}, **cloud_secret_sources}
    }
    candidate = copy.deepcopy(active)
    candidate["secrets"]["exchange-tls-cert"] = {"file": str(candidate_source)}
    candidate["services"] = {"cloud-management-phase2": {"secrets": cloud_mounts}}

    with pytest.raises(
        stage_transition.StageTransitionError, match="changes unrelated secret source"
    ):
        stage_transition._validate_active_candidate_secrets(active, candidate)


def test_provider_bootstrap_uses_exact_service_delta_and_order() -> None:
    """Add only HF and cloud service while upgrading persistence and the single Navigator."""

    active, candidate, active_lock, candidate_lock = _active_and_bootstrap()

    targets, images = stage_transition._validate_phase_topology(
        phase="provider-bootstrap",
        active=active,
        candidate=candidate,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )

    assert targets == [
        "navigator-persistence",
        "account-center",
        "huggingface",
        "cloud-management-phase2",
        "exchange",
        "navigator",
    ]
    assert images["huggingface"]["added"] is True
    assert images["cloud-management-phase2"]["added"] is True
    assert (
        images["navigator-persistence"]["active"]["imageId"]
        != images["navigator-persistence"]["candidate"]["imageId"]
    )


def test_provider_bootstrap_continuation_keeps_already_migrated_persistence_image() -> None:
    """Continue from the observed mixed baseline without recreating Persistence by image."""

    active, candidate, _active_lock, candidate_lock = _active_and_bootstrap()
    active["services"]["navigator-persistence"]["image"] = "persistence:v2"
    active["services"]["account-center"]["environment"]["HF_ENABLED"] = "true"
    active_lock = _lock(
        [
            ("navigator-persistence", "persistence:v2", 7),
            ("dsh-executor", "navigator:v1", 2),
            ("account-center", "account-center:v1", 3),
            ("exchange", "exchange:v1", 4),
            ("teams-ingress", "teams-ingress:v1", 5),
            ("teams-public-ingress", "caddy:v1", 6),
        ]
    )

    targets, images = stage_transition._validate_phase_topology(
        phase="provider-bootstrap",
        active=active,
        candidate=candidate,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )

    assert "navigator-persistence" not in targets
    assert images["navigator-persistence"]["active"] == images["navigator-persistence"]["candidate"]
    assert {"huggingface", "cloud-management-phase2", "navigator"} <= set(targets)


def test_provider_bootstrap_keeps_runpod_as_an_optional_cloud_profile() -> None:
    """The phase-two cloud service remains active with Azure/GCP before RunPod is configured."""

    active, candidate, active_lock, candidate_lock = _active_and_bootstrap()
    candidate["services"]["cloud-management-phase2"]["profiles"] = ["cloud-phase2", "runpod"]
    targets, _images = stage_transition._validate_phase_topology(
        phase="provider-bootstrap",
        active=active,
        candidate=candidate,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )
    assert "cloud-management-phase2" in targets


def test_provider_bootstrap_rejects_an_independent_executor() -> None:
    """Do not let a profile change add any alternate runtime/agent service."""

    active, candidate, active_lock, candidate_lock = _active_and_bootstrap()
    candidate["services"]["second-navigator"] = _service("navigator:v1")
    with pytest.raises(stage_transition.StageTransitionError, match="add only Hugging Face"):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def test_provider_bootstrap_preserves_the_database_and_codebuddy_mounts() -> None:
    """Reject a new SQLite volume or CodeBuddy profile path before planning a cutover."""

    active, candidate, active_lock, candidate_lock = _active_and_bootstrap()
    candidate["services"]["navigator-persistence"]["volumes"][0]["source"] = "new-db"
    with pytest.raises(stage_transition.StageTransitionError, match="navigator-sessions volume"):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_and_bootstrap()
    candidate["services"]["navigator"]["volumes"][1]["source"] = "/private/new-codebuddy"
    with pytest.raises(stage_transition.StageTransitionError, match="CodeBuddy profile path"):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def test_mixed_mount_identity_uses_candidate_mounts_only_for_recreated_services(
    tmp_path: Path,
) -> None:
    """Keep old static config mounts on unselected services across a targeted cutover."""

    old_config = tmp_path / "active" / "navigator.json"
    new_config = tmp_path / "candidate" / "navigator.json"
    codebuddy_home = tmp_path / "codebuddy"
    for path in (old_config, new_config):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.parent.name, encoding="utf-8")
    codebuddy_home.mkdir()
    (codebuddy_home / "session.json").write_text("mutable", encoding="utf-8")

    def services(config_file: Path) -> dict[str, Any]:
        """Build one active runtime whose executor retains its persistent profile volume."""

        ingress = _service("ingress:v1", profiles=["public-ingress"])
        ingress["volumes"] = [
            {
                "type": "bind",
                "source": str(config_file),
                "target": "/etc/navigator/config.json",
                "read_only": True,
            }
        ]
        return {
            "navigator-persistence": _service("persistence:v1"),
            "navigator": {
                **_mount_service("navigator:v1", executor=True),
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(config_file),
                        "target": "/etc/navigator/config.json",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": str(codebuddy_home),
                        "target": "/home/navigator/.codebuddy",
                    },
                ],
            },
            "teams-ingress": ingress,
        }

    active = {"name": "cyrene-teams-cloud", "services": services(old_config), "volumes": {}}
    candidate = {
        "name": "cyrene-teams-cloud",
        "services": services(new_config),
        "volumes": {},
    }
    active_identity = stage_transition._runtime_config_identity_for_compose(active)
    candidate_identity = stage_transition._candidate_runtime_config_identity(
        active_identity=active_identity,
        candidate_compose=candidate,
        target_services={"navigator"},
    )

    navigator_mounts = candidate_identity["navigator"]
    edge_mounts = candidate_identity["teams-ingress"]
    old_digest = stage_transition._sha256_file(old_config)
    new_digest = stage_transition._sha256_file(new_config)

    def config_mount(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return next(row for row in rows if row["target"] == "/etc/navigator/config.json")

    assert config_mount(navigator_mounts)["source"] == str(new_config)
    assert config_mount(navigator_mounts)["contentSha256"] == new_digest
    assert config_mount(edge_mounts)["source"] == str(old_config)
    assert config_mount(edge_mounts)["contentSha256"] == old_digest
    assert not any(
        "contentSha256" in row for row in navigator_mounts if row["target"].endswith(".codebuddy")
    )

    (codebuddy_home / "session.json").write_text(
        "mutated while service stays live", encoding="utf-8"
    )
    stage_transition._verify_runtime_config_identity_sources(
        candidate_identity, "candidate runtime configuration"
    )


def test_secret_mount_identity_uses_docker_destination_for_bare_compose_targets(
    tmp_path: Path,
) -> None:
    """Normalize Compose secret leaves to Docker's absolute destination and keep configs literal."""

    secret_a = tmp_path / "teams-delivery-admin-token"
    secret_b = tmp_path / "exchange-tls-cert"
    secret_c = tmp_path / "absolute-secret-target"
    config_file = tmp_path / "connector-bootstrap.json"
    for path, content in (
        (secret_a, "private token"),
        (secret_b, "public certificate"),
        (secret_c, "absolute secret"),
        (config_file, "{}"),
    ):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o400)
    compose = {
        "name": "cyrene-teams-cloud",
        "services": {
            "exchange": {
                "secrets": [
                    {
                        "source": "teams-delivery-admin-token",
                        "target": "teams-delivery-admin-token",
                    },
                    {"source": "exchange-tls-cert", "target": "exchange-tls-cert"},
                    {
                        "source": "absolute-secret-target",
                        "target": "/etc/credentials/absolute-secret",
                    },
                ],
                "configs": [
                    {
                        "source": "connector-bootstrap",
                        "target": "/run/config/connector-bootstrap.json",
                    }
                ],
            }
        },
        "secrets": {
            "teams-delivery-admin-token": {"file": str(secret_a)},
            "exchange-tls-cert": {"file": str(secret_b)},
            "absolute-secret-target": {"file": str(secret_c)},
        },
        "configs": {"connector-bootstrap": {"file": str(config_file)}},
    }

    identity = stage_transition._expected_mount_identity(compose, "exchange")
    assert {row["target"] for row in identity} == {
        "/run/secrets/teams-delivery-admin-token",
        "/run/secrets/exchange-tls-cert",
        "/etc/credentials/absolute-secret",
        "/run/config/connector-bootstrap.json",
    }
    stage_transition._validate_runtime_config_identity(
        {"exchange": identity},
        expected_services={"exchange"},
        label="test runtime identity",
    )
    actual_docker_mounts = [
        {
            "Type": "bind",
            "Source": str(path.resolve()),
            "Destination": target,
            "RW": False,
        }
        for path, target in (
            (secret_a, "/run/secrets/teams-delivery-admin-token"),
            (secret_b, "/run/secrets/exchange-tls-cert"),
            (secret_c, "/etc/credentials/absolute-secret"),
            (config_file, "/run/config/connector-bootstrap.json"),
        )
    ]
    assert stage_transition._service_mounts_match(actual_docker_mounts, identity)
    actual_docker_mounts[0]["Destination"] = "teams-delivery-admin-token"
    assert not stage_transition._service_mounts_match(actual_docker_mounts, identity)

    duplicate = json.loads(json.dumps(compose))
    duplicate["services"]["exchange"]["secrets"].append(
        {"source": "exchange-tls-cert", "target": "/run/secrets/teams-delivery-admin-token"}
    )
    duplicate_identity = stage_transition._expected_mount_identity(duplicate, "exchange")
    with pytest.raises(stage_transition.StageTransitionError, match="mount identity is malformed"):
        stage_transition._validate_runtime_config_identity(
            {"exchange": duplicate_identity},
            expected_services={"exchange"},
            label="duplicate runtime identity",
        )

    ambiguous = json.loads(json.dumps(compose))
    ambiguous["services"]["exchange"]["secrets"][0]["target"] = "nested/token"
    with pytest.raises(stage_transition.StageTransitionError, match="secret target is malformed"):
        stage_transition._expected_mount_identity(ambiguous, "exchange")


def test_runtime_selection_v2_pins_static_config_but_not_live_profile_contents(
    tmp_path: Path,
) -> None:
    """Accept normal profile writes while rejecting static config drift under a selector."""

    selection_dir = tmp_path / "selection"
    selection_dir.mkdir(mode=0o700)
    config_dir = tmp_path / "config"
    secrets_dir = tmp_path / "secrets"
    config_dir.mkdir(mode=0o700)
    secrets_dir.mkdir(mode=0o700)
    static_file = tmp_path / "navigator-static.json"
    static_file.write_text("fixed", encoding="utf-8")
    profile_dir = tmp_path / "codebuddy"
    profile_dir.mkdir(mode=0o700)
    profile_file = profile_dir / "session.json"
    profile_file.write_text("first session", encoding="utf-8")
    seed = tmp_path / "hf-seed.json"
    runpod_key = tmp_path / "runpod-key"
    seed.write_text("opaque seed fixture", encoding="utf-8")
    runpod_key.write_text("opaque key fixture", encoding="utf-8")

    static_metadata = static_file.stat()
    profile_metadata = profile_dir.stat()
    identity = {
        "navigator": [
            {
                "type": "bind",
                "source": str(static_file.resolve()),
                "target": "/etc/navigator/static.json",
                "readOnly": True,
                "sourceKind": "file",
                "sourceUid": static_metadata.st_uid,
                "sourceMode": stat.S_IMODE(static_metadata.st_mode),
                "contentSha256": stage_transition._sha256_file(static_file),
            },
            {
                "type": "bind",
                "source": str(profile_dir.resolve()),
                "target": "/home/navigator/.codebuddy",
                "readOnly": False,
                "sourceKind": "directory",
                "sourceUid": profile_metadata.st_uid,
                "sourceMode": 0o700,
            },
        ],
    }
    compose_files = [{"path": str((tmp_path / "compose.yaml").resolve()), "sha256": "a" * 64}]
    env_files = [{"path": str((tmp_path / "runtime.env").resolve()), "sha256": "b" * 64}]
    image_lock = {"path": str((tmp_path / "images.json").resolve()), "sha256": "c" * 64}
    active_inputs = {"composeFiles": compose_files, "envFiles": env_files, "imageLock": image_lock}
    document = {
        "schemaVersion": stage_transition.SELECTION_SCHEMA,
        "project": "cyrene-teams-cloud",
        "operationId": "00000000-0000-4000-8000-000000000001",
        "planSha256": "d" * 64,
        "phase": "provider-bootstrap",
        "composeFiles": compose_files,
        "envFiles": env_files,
        "imageLock": image_lock,
        "secretsDir": str(secrets_dir.resolve()),
        "configDir": str(config_dir.resolve()),
        "huggingfaceSeed": stage_transition._file_record(seed, "test HF seed"),
        "runpodKey": stage_transition._file_record(runpod_key, "test RunPod key"),
        "runtimeConfigIdentity": identity,
        "updatedAtUtc": "2026-10-26T12:00:00+00:00",
    }
    selection_path = selection_dir / "active-runtime-selection.json"
    selection_path.write_text(json.dumps(document), encoding="utf-8")
    selection_path.chmod(0o600)

    selected = stage_transition._selection_file_state(
        selection_path,
        active_inputs,
        project="cyrene-teams-cloud",
        config_dir=config_dir,
        secrets_dir=secrets_dir,
        active_service_names={"navigator"},
    )
    assert selected["document"]["runtimeConfigIdentity"] == identity

    profile_file.write_text("continued session", encoding="utf-8")
    stage_transition._selection_file_state(
        selection_path,
        active_inputs,
        project="cyrene-teams-cloud",
        config_dir=config_dir,
        secrets_dir=secrets_dir,
        active_service_names={"navigator"},
    )
    static_file.write_text("changed static config", encoding="utf-8")
    with pytest.raises(stage_transition.StageTransitionError, match="static config source changed"):
        stage_transition._selection_file_state(
            selection_path,
            active_inputs,
            project="cyrene-teams-cloud",
            config_dir=config_dir,
            secrets_dir=secrets_dir,
            active_service_names={"navigator"},
        )


def test_activation_is_config_only_and_keeps_every_immutable_image() -> None:
    """Issued connector activation cannot silently rebuild or upgrade a service image."""

    _active, candidate, _active_lock, candidate_lock = _active_and_bootstrap()
    active = {
        **candidate,
        "services": {name: dict(row) for name, row in candidate["services"].items()},
    }
    active["services"]["navigator"]["environment"] = {}
    targets, _images = stage_transition._validate_phase_topology(
        phase="issued-connector-activation",
        active=active,
        candidate=candidate,
        active_lock=candidate_lock,
        candidate_lock=candidate_lock,
    )
    assert "navigator" in targets

    changed_lock = copy.deepcopy(candidate_lock)
    cloud_row = next(row for row in changed_lock["images"] if row["service"] == "cloud-management")
    cloud_row["imageId"] = _image_id(42)
    with pytest.raises(
        stage_transition.StageTransitionError, match="keep every image ID unchanged"
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            active=active,
            candidate=candidate,
            active_lock=candidate_lock,
            candidate_lock=changed_lock,
        )


def test_selection_promotion_unknown_state_is_never_replayed(tmp_path: Path) -> None:
    """A changed pointer after a recorded promotion intent requires manual recovery."""

    selection = tmp_path / "active-runtime-selection.json"
    selection.write_text('{"schemaVersion":"old"}', encoding="utf-8")
    old_digest = stage_transition._sha256_file(selection)
    selection.write_text('{"schemaVersion":"new"}', encoding="utf-8")
    journal = {
        "selection": {
            "path": str(selection),
            "oldSha256": old_digest,
            "newSha256": stage_transition._sha256_file(selection),
            "promotionIntent": True,
            "promoted": False,
        }
    }
    assert stage_transition._selection_promotion_uncertain(journal) is True


def test_same_plan_apply_rechecks_journal_under_lock_and_never_overwrites_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller queued behind an ambiguous apply must observe, not replace, its journal."""

    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(mode=0o700)
    selection_path = tmp_path / "active-runtime-selection.json"
    plan = {
        "schemaVersion": stage_transition.PLAN_SCHEMA,
        "operationId": "00000000-0000-4000-8000-000000000001",
        "phase": "provider-bootstrap",
        "createdAtUtc": "2026-10-26T12:00:00+00:00",
        "project": "cyrene-teams-cloud",
        "activeInputs": {},
        "candidateInputs": {},
        "candidateInputPaths": {},
        "activeConfigDir": str(tmp_path),
        "candidateConfigDir": str(tmp_path),
        "activeSecretsDir": str(tmp_path),
        "journalDir": str(journal_dir),
        "candidateSelectionFile": str(selection_path),
        "selectionBackupPath": str(tmp_path / "previous.json"),
        "activeSelection": {"path": str(selection_path), "sha256": None, "document": None},
        "runtimeConfigIdentity": {
            "active": {"navigator": []},
            "candidate": {"navigator": []},
        },
        "candidateServices": {"navigator": {"added": False}},
        "targets": ["navigator"],
        "activeServices": {"navigator": {}},
        "edgeDelivery": {},
        "navigatorDrain": {},
        "database": {},
        "exchangeReadiness": {
            "required": False,
            "status": "NOT_REQUIRED_PROVIDER_BOOTSTRAP",
        },
        "candidatePreflight": {
            "mode": "connector-auth-readonly",
            "ingressExposure": "public-cutover",
            "hostCaddyBinary": "/usr/local/bin/caddy",
            "opensslBinary": "openssl",
            "hostFrontedIngressReadiness": None,
            "credentialReadOnlyRuntimeReady": True,
            "fullCloudWriteDeploymentReady": False,
            "lifecycleAcceptance": "NOT_RUN",
        },
        "policy": {"dockerPrefix": ["docker"]},
    }
    plan["planSha256"] = stage_transition._sha256_bytes(stage_transition._canonical_json(plan))
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(stage_transition._canonical_json(plan))
    plan_path.chmod(0o600)
    journal_path = stage_transition._journal_path(journal_dir, plan["operationId"])

    @contextmanager
    def competing_operation_lock(_project: str):
        journal_path.write_text('{"state":"RECOVERY_REQUIRED"}', encoding="utf-8")
        journal_path.chmod(0o600)
        yield

    monkeypatch.setattr(stage_transition.runtime_update, "_require_update_operator", lambda: None)
    monkeypatch.setattr(stage_transition.preflight, "_private_artifact", lambda _path: True)
    monkeypatch.setattr(
        stage_transition.runtime_update, "_ensure_journal_directory", lambda *_a, **_k: None
    )
    monkeypatch.setattr(stage_transition.runtime_update, "_validate_docker_prefix", lambda _p: None)
    monkeypatch.setattr(
        stage_transition.runtime_update, "_operation_lock", competing_operation_lock
    )
    monkeypatch.setattr(
        stage_transition,
        "_verify_all_inputs",
        lambda _plan: pytest.fail("a competing journal must stop before reconciliation"),
    )

    with pytest.raises(
        stage_transition.StageTransitionError, match="already exists; apply is never replayed"
    ):
        stage_transition.apply_plan_file(plan_path, expected_digest=plan["planSha256"])
    assert journal_path.read_text(encoding="utf-8") == '{"state":"RECOVERY_REQUIRED"}'


def test_initial_journal_creation_is_exclusive_and_does_not_replace_existing_bytes(
    tmp_path: Path,
) -> None:
    """Use create-exclusive semantics for the first journal write."""

    journal = tmp_path / "operation.json"
    journal.write_text('{"state":"RECOVERY_REQUIRED"}', encoding="utf-8")
    with pytest.raises(stage_transition.StageTransitionError, match="already exists"):
        stage_transition._create_initial_journal(journal, {"state": "RUNNING"})
    assert journal.read_text(encoding="utf-8") == '{"state":"RECOVERY_REQUIRED"}'


def test_final_ingress_readback_rejects_a_new_dead_letter_before_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not publish APPLIED when the resumed spool reports a new dead-letter event."""

    journal = {
        "edge": {
            "containerId": "3" * 64,
            "initialDeadCount": 0,
            "latestDeadCount": 0,
            "resumeRevision": 2,
        }
    }
    status = {
        "paused": False,
        "revision": 2,
        "active_delivery_count": 0,
        "queued_count": 0,
        "leased_count": 0,
        "retry_wait_count": 0,
        "delivered_count": 1,
        "rejected_count": 0,
        "dead_count": 1,
        "pending_bytes": 0,
        "retained_body_bytes": 0,
        "oldest_pending_at": None,
        "last_delivered_at": None,
    }
    requested_urls = _install_internal_status_transport(monkeypatch, edge_status=status)

    with pytest.raises(runtime_update.RuntimeUpdateError, match="dead-letter count changed"):
        stage_transition._verify_final_delivery_readback(journal=journal, docker_prefix=["docker"])
    assert journal["edge"]["latestDeadCount"] == 1
    assert len(requested_urls) == 1


def _table_report(
    *,
    name: str,
    row_count: int,
    rows_digest: str,
    preserved_count: int | None = None,
    preserved_digest: str | None = None,
) -> dict[str, Any]:
    """Build one bounded schema/data report for the migration-gate fixtures."""

    columns = [
        {
            "name": column,
            "type": "TEXT",
            "notNull": True,
            "default": None,
            "primaryKeyOrdinal": ordinal,
            "hidden": 0,
        }
        for ordinal, column in enumerate(("organization_id", "workspace_id", "session_id"), 1)
    ]
    report: dict[str, Any] = {
        "columns": columns,
        "createSql": (
            f"CREATE TABLE {name} (organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, "
            "session_id TEXT NOT NULL, PRIMARY KEY (organization_id, workspace_id, session_id))"
        ),
        "primaryKeyColumns": ["organization_id", "workspace_id", "session_id"],
        "foreignKeys": [],
        "checkConstraints": [],
        "uniqueIndexes": [],
        "rowCount": row_count,
        "rowsDigest": rows_digest,
        "emptyOrganizationIdRows": row_count,
    }
    if preserved_count is not None:
        report["preservedRowsCount"] = preserved_count
        report["preservedRowsDigest"] = preserved_digest
    return report


def _sqlite_report_fixture(
    tables: dict[str, dict[str, Any]],
    *,
    session_sql: str = (
        "CREATE TABLE sessions (organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, "
        "session_id TEXT NOT NULL, PRIMARY KEY (organization_id, workspace_id, session_id))"
    ),
    user_version: int = 0,
) -> dict[str, Any]:
    """Recompute exact schema digest metadata for a synthetic migration readback."""

    for name, row in tables.items():
        if name == "sessions":
            row["createSql"] = session_sql
        elif name == "work_credential_authorization_health":
            row["createSql"] = stage_transition.SQLITE_CREDENTIAL_HEALTH_TABLE_SQL

    schema_objects = [
        {
            "type": "table",
            "name": name,
            "sql": row["createSql"],
        }
        for name, row in sorted(tables.items())
    ]
    schema_digest = stage_transition._sha256_bytes(stage_transition._canonical_json(schema_objects))
    report = {
        "integrity": "ok",
        "userVersion": user_version,
        "schemaSha256": schema_digest,
        "objects": sorted(f"table:{name}" for name in tables),
        "schemaObjects": schema_objects,
        "tables": tables,
    }
    stage_transition._validate_sqlite_report_shape(report, "fixture SQLite report")
    return report


def _work_credential_health_table_report() -> dict[str, Any]:
    """Return the exact empty additive table contract exported by Navigator 17700."""

    columns = [
        ("organization_id", "TEXT", True, None, 1),
        ("workspace_id", "TEXT", True, None, 2),
        ("connection_id", "TEXT", True, None, 3),
        ("provider", "TEXT", True, None, 0),
        ("generation", "INTEGER", True, None, 0),
        ("fingerprint", "TEXT", True, None, 0),
        ("decision", "TEXT", True, None, 0),
        ("checked_at", "TEXT", False, None, 0),
        ("expires_at", "TEXT", False, None, 0),
        ("reason_code", "TEXT", False, None, 0),
        ("incident_id", "TEXT", False, None, 0),
        ("incident_started_at", "INTEGER", False, None, 0),
        ("last_notified_at", "INTEGER", False, None, 0),
        ("reminder_count", "INTEGER", True, "0", 0),
        ("updated_at", "INTEGER", True, None, 0),
    ]
    return {
        "columns": [
            {
                "name": name,
                "type": type_name,
                "notNull": not_null,
                "default": default,
                "primaryKeyOrdinal": ordinal,
                "hidden": 0,
            }
            for name, type_name, not_null, default, ordinal in columns
        ],
        "createSql": stage_transition.SQLITE_CREDENTIAL_HEALTH_TABLE_SQL,
        "primaryKeyColumns": ["organization_id", "workspace_id", "connection_id"],
        "foreignKeys": [],
        "checkConstraints": [],
        "uniqueIndexes": [],
        "rowCount": 0,
        "rowsDigest": hashlib.sha256(b"").hexdigest(),
        "emptyOrganizationIdRows": 0,
    }


def test_sqlite_migration_preserves_old_ddl_and_rows_while_adding_only_health_table() -> None:
    """The old image keeps its exact tables/data; only the pinned empty health table may appear."""

    session_rows_digest = "a" * 64
    before = _sqlite_report_fixture(
        {"sessions": _table_report(name="sessions", row_count=2, rows_digest=session_rows_digest)}
    )
    current_sessions = _table_report(
        name="sessions",
        row_count=2,
        rows_digest=session_rows_digest,
        preserved_count=2,
        preserved_digest=session_rows_digest,
    )
    current_health = _work_credential_health_table_report()
    current = _sqlite_report_fixture(
        {
            "sessions": current_sessions,
            "work_credential_authorization_health": current_health,
        }
    )

    stage_transition._validate_database_migration_reports(before, current)
    assert current["migrationCompatibility"]["class"] == (
        "ADDITIVE_ONLY_EXISTING_SCHEMA_AND_ROWS_PRESERVED"
    )
    assert current["migrationCompatibility"]["preservedExistingTables"] == ["sessions"]


def test_sqlite_migration_rejects_same_name_ddl_or_row_content_changes() -> None:
    """Same object names cannot hide a table rewrite, lost rows, or changed old values."""

    old_digest = "a" * 64
    before = _sqlite_report_fixture(
        {"sessions": _table_report(name="sessions", row_count=2, rows_digest=old_digest)}
    )
    changed_ddl = _sqlite_report_fixture(
        {
            "sessions": _table_report(
                name="sessions",
                row_count=2,
                rows_digest=old_digest,
                preserved_count=2,
                preserved_digest=old_digest,
            )
        },
        session_sql="CREATE TABLE sessions (session_id INTEGER PRIMARY KEY)",
    )
    with pytest.raises(stage_transition.StageTransitionError, match="changed the DDL"):
        stage_transition._validate_database_migration_reports(before, changed_ddl)

    lost_rows = _sqlite_report_fixture(
        {
            "sessions": _table_report(
                name="sessions",
                row_count=1,
                rows_digest="b" * 64,
                preserved_count=1,
                preserved_digest="b" * 64,
            )
        }
    )
    with pytest.raises(
        stage_transition.StageTransitionError, match="changed or lost pre-existing rows"
    ):
        stage_transition._validate_database_migration_reports(before, lost_rows)


def test_sqlite_migration_rejects_undeclared_new_tables_and_never_restores_backup() -> None:
    """Unknown database additions fail closed and snapshot restoration remains forbidden."""

    session_rows_digest = "a" * 64
    before = _sqlite_report_fixture(
        {"sessions": _table_report(name="sessions", row_count=1, rows_digest=session_rows_digest)}
    )
    current = _sqlite_report_fixture(
        {
            "sessions": _table_report(
                name="sessions",
                row_count=1,
                rows_digest=session_rows_digest,
                preserved_count=1,
                preserved_digest=session_rows_digest,
            ),
            "unexpected": _table_report(name="unexpected", row_count=0, rows_digest="c" * 64),
        }
    )
    with pytest.raises(
        stage_transition.StageTransitionError, match="undeclared SQLite schema object"
    ):
        stage_transition._validate_database_migration_reports(before, current)
    assert before["integrity"] == "ok"


def test_sqlite_migration_pins_exact_additive_health_table_ddl() -> None:
    """A table with matching names/columns but changed additive DDL is rejected."""

    before = _sqlite_report_fixture(
        {"sessions": _table_report(name="sessions", row_count=0, rows_digest="a" * 64)}
    )
    current = _sqlite_report_fixture(
        {
            "sessions": _table_report(
                name="sessions",
                row_count=0,
                rows_digest="a" * 64,
                preserved_count=0,
                preserved_digest="a" * 64,
            ),
            "work_credential_authorization_health": _work_credential_health_table_report(),
        }
    )
    new_ddl = current["tables"]["work_credential_authorization_health"]["createSql"].replace(
        "reminder_count INTEGER NOT NULL DEFAULT 0",
        "reminder_count INTEGER NOT NULL DEFAULT 1",
    )
    current["tables"]["work_credential_authorization_health"]["createSql"] = new_ddl
    schema_record = next(
        row
        for row in current["schemaObjects"]
        if row["name"] == "work_credential_authorization_health"
    )
    schema_record["sql"] = new_ddl
    current["schemaSha256"] = stage_transition._sha256_bytes(
        stage_transition._canonical_json(current["schemaObjects"])
    )

    with pytest.raises(
        stage_transition.StageTransitionError, match="pinned Navigator 17700 schema"
    ):
        stage_transition._validate_database_migration_reports(before, current)


def test_sqlite_migration_accepts_recreated_persistence_with_existing_schema_unchanged() -> None:
    """A same-image Persistence recreate can pass the existing 25-to-25 preservation gate."""

    sessions = _table_report(name="sessions", row_count=1, rows_digest="a" * 64)
    health = _work_credential_health_table_report()
    health["rowCount"] = 2
    health["rowsDigest"] = "b" * 64
    before = _sqlite_report_fixture(
        {"sessions": sessions, "work_credential_authorization_health": health}
    )
    current_tables = copy.deepcopy(before["tables"])
    for record in current_tables.values():
        record["preservedRowsCount"] = record["rowCount"]
        record["preservedRowsDigest"] = record["rowsDigest"]
    current = _sqlite_report_fixture(current_tables)

    stage_transition._validate_database_migration_reports(before, current)

    assert current["migrationCompatibility"]["addedSchemaObjects"] == []
    assert current["migrationCompatibility"]["preservedExistingTables"] == [
        "sessions",
        "work_credential_authorization_health",
    ]


def test_preexisting_persistence_candidate_requires_exact_health_schema_but_allows_rows() -> None:
    """A reused candidate image requires the exact migrated table without assuming it is empty."""

    image = {"imageRef": "persistence:v2", "imageId": _image_id(24)}
    plan = {
        "phase": "provider-bootstrap",
        "candidateServices": {"navigator-persistence": {"active": image, "candidate": image}},
    }
    sessions = _table_report(name="sessions", row_count=1, rows_digest="a" * 64)
    health = _work_credential_health_table_report()
    health["rowCount"] = 2
    health["rowsDigest"] = "b" * 64
    report = _sqlite_report_fixture(
        {"sessions": sessions, "work_credential_authorization_health": health}
    )

    checked = stage_transition._validate_bootstrap_persistence_baseline(plan, report)
    assert checked is not None

    assert checked["tables"]["work_credential_authorization_health"]["rowCount"] == 2
    assert checked["schemaSha256"] == report["schemaSha256"]

    missing = _sqlite_report_fixture(
        {"sessions": _table_report(name="sessions", row_count=1, rows_digest="a" * 64)}
    )
    with pytest.raises(
        stage_transition.StageTransitionError, match="lacks its credential-health table"
    ):
        stage_transition._validate_bootstrap_persistence_baseline(plan, missing)

    bad_ddl = copy.deepcopy(report)
    bad_ddl["tables"]["work_credential_authorization_health"]["createSql"] = bad_ddl["tables"][
        "work_credential_authorization_health"
    ]["createSql"].replace(
        "reminder_count INTEGER NOT NULL DEFAULT 0",
        "reminder_count INTEGER NOT NULL DEFAULT 1",
    )
    ddl = bad_ddl["tables"]["work_credential_authorization_health"]["createSql"]
    schema_row = next(
        row
        for row in bad_ddl["schemaObjects"]
        if row["name"] == "work_credential_authorization_health"
    )
    schema_row["sql"] = ddl
    bad_ddl["schemaSha256"] = stage_transition._sha256_bytes(
        stage_transition._canonical_json(bad_ddl["schemaObjects"])
    )
    with pytest.raises(
        stage_transition.StageTransitionError, match="pinned Navigator 17700 schema"
    ):
        stage_transition._validate_bootstrap_persistence_baseline(plan, bad_ddl)

    initial_upgrade = {
        "phase": "provider-bootstrap",
        "candidateServices": {
            "navigator-persistence": {
                "active": {"imageId": _image_id(1)},
                "candidate": {"imageId": _image_id(2)},
            }
        },
    }
    assert (
        stage_transition._validate_bootstrap_persistence_baseline(initial_upgrade, missing) is None
    )


def _exchange_readiness_fixture(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one redacted Exchange e704358 readiness snapshot and matching candidate inputs."""

    actor = {
        "issuer": "https://login.microsoftonline.com/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/v2.0",
        "subject": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "organizationId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "workspaceId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    }
    hf_profile_ref = "11111111-1111-4111-8111-111111111111"
    repositories = [
        {"repoType": "model", "repoId": "Qwen/Qwen2.5-0.5B-Instruct", "permissions": ["read"]}
    ]
    cloud_profiles = {
        "hf-primary": {
            "provider": "hf",
            "authMode": "device_oauth",
            "accountScopes": {"repositories": repositories},
            "targets": {"hf-model": {"targetRef": repositories[0]["repoId"]}},
        },
        "azure-primary": {
            "provider": "azure",
            "identityRef": "azure-identity",
            "accessMode": "workspace_operator",
            "targets": {"azure-read": {"targetRef": "azure-target-1"}},
        },
        "gcp-primary": {
            "provider": "gcp",
            "identityRef": "gcp-identity",
            "accessMode": "workspace_operator",
            "targets": {"gcp-read": {"targetRef": "gcp-target-1"}},
        },
    }
    identifiers = {
        "huggingface": ("22222222-2222-4222-8222-222222222222", hf_profile_ref),
        "azure": ("33333333-3333-4333-8333-333333333333", "azure-primary"),
        "gcp": ("44444444-4444-4444-8444-444444444444", "gcp-primary"),
    }
    bindings = []
    catalog_items = []
    profiles = []
    connections = []
    for provider_id, (connection_ref, profile_ref) in identifiers.items():
        binding_id = f"exchange-{provider_id}-{connection_ref}"
        tool_id = f"{provider_id}.readonly.v1"
        bindings.append(
            {
                "bindingId": binding_id,
                "connectionRef": connection_ref,
                "providerId": provider_id,
                "profileRef": profile_ref,
                "tools": [
                    {
                        "toolId": tool_id,
                        "operationId": f"{provider_id}.list",
                        "resourceProjection": "safe_summary",
                        "mutating": False,
                    }
                ],
            }
        )
        catalog_items.append(
            {
                "bindingId": binding_id,
                "capabilityId": "tool.provider.v1",
                "providerId": provider_id,
                "connectionRef": connection_ref,
                "profileRef": profile_ref,
                "tools": [
                    {
                        "id": tool_id,
                        "name": f"Read {provider_id}",
                        "description": "Read one scoped safe summary.",
                        "readOnly": True,
                        "inputSchema": {"type": "object", "additionalProperties": False},
                    }
                ],
            }
        )
        if provider_id == "huggingface":
            profile_alias = "hf-primary"
            profile = {
                "profileRef": profile_ref,
                "profileAlias": profile_alias,
                "providerId": provider_id,
                "authStatus": "connected",
                "accessMode": "workspace_operator",
                "authMode": "device_oauth",
                "targetScope": {"repositories": repositories},
            }
            connection = {
                "connectionRef": connection_ref,
                "providerId": provider_id,
                "profileRef": profile_ref,
                "profileAlias": profile_alias,
                "targetAlias": "hf-model",
                "accessMode": "workspace_operator",
                "readOnly": True,
                "targetScope": repositories[0],
            }
        else:
            profile_alias = f"{provider_id}-primary"
            target_alias = f"{provider_id}-read"
            target_ref = f"{provider_id}-target-1"
            profile = {
                "profileRef": profile_ref,
                "profileAlias": profile_alias,
                "providerId": provider_id,
                "accessMode": "workspace_operator",
                "targetScope": {"targets": {target_alias: {"targetRef": target_ref}}},
                "readOnly": True,
            }
            connection = {
                "connectionRef": connection_ref,
                "providerId": provider_id,
                "profileRef": profile_ref,
                "profileAlias": profile_alias,
                "targetAlias": target_alias,
                "accessMode": "workspace_operator",
                "readOnly": True,
                "targetScope": {"targetAlias": target_alias, "targetRef": target_ref},
            }
        profiles.append(profile)
        connections.append(connection)

    bindings_file = tmp_path / "connector-bindings.json"
    catalog_file = tmp_path / "connector-tool-catalog.json"
    bootstrap_file = tmp_path / "connector-bootstrap.json"
    seed_file = tmp_path / "huggingface-oauth.json"
    bindings_file.write_text(json.dumps({"schemaVersion": 1, "bindings": bindings}))
    catalog_file.write_text(json.dumps({"schemaVersion": 1, "items": catalog_items}))
    bootstrap_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "huggingface": {
                    "profileAlias": "hf-primary",
                    "expectedAccessMode": "workspace_operator",
                },
            }
        )
    )
    seed_file.write_text(json.dumps({"auth_mode": "device_oauth"}))
    compose = {
        "configs": {
            "connector-bindings": {"file": str(bindings_file)},
            "connector-tool-catalog": {"file": str(catalog_file)},
            "connector-bootstrap": {"file": str(bootstrap_file)},
        },
        "secrets": {"account-center-huggingface-oauth-source": {"file": str(seed_file)}},
        "services": {
            "navigator": {
                "environment": {"TEAMS_TENANT_ID": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
            },
            "directory-operator": {
                "environment": {"CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID": actor["subject"]}
            },
            "workspace-authority": {
                "environment": {
                    "CYRENE_ORGANIZATION_ID": actor["organizationId"],
                    "CYRENE_WORKSPACE_ID": actor["workspaceId"],
                }
            },
            "cloud-management-phase2": {
                "environment": {
                    "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
                        {
                            "profiles": [
                                {
                                    "provider": "azure",
                                    "profile_ref": "azure-primary",
                                    "identity_ref": "azure-identity",
                                    "read_only": True,
                                },
                                {
                                    "provider": "gcp",
                                    "profile_ref": "gcp-primary",
                                    "identity_ref": "gcp-identity",
                                    "read_only": True,
                                },
                            ]
                        }
                    )
                }
            },
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                        {"authorizationCloud": {"profiles": cloud_profiles}}
                    )
                }
            },
        },
    }
    snapshot = {
        "schemaVersion": 1,
        "actor": actor,
        "profiles": profiles,
        "connections": connections,
        "bindings": bindings,
        "catalog": {"schemaVersion": 1, "items": catalog_items},
    }
    return snapshot, compose


def test_bootstrap_exchange_image_readiness_preserves_only_existing_bindings(
    tmp_path: Path,
) -> None:
    """A bootstrap-phase image update verifies the current catalog without requiring future providers."""

    snapshot, compose = _exchange_readiness_fixture(tmp_path)
    bindings: list[dict[str, Any]] = []
    catalog_items: list[dict[str, Any]] = []
    existing = [("github", "github-connection", "github-profile", index) for index in range(10)]
    existing.append(("computer", "computer-connection", "computer-profile", 10))
    for provider, connection_ref, profile_ref, index in existing:
        binding_id = f"legacy-{provider}-{index}"
        tool_id = f"{provider}.readonly.{index}"
        bindings.append(
            {
                "bindingId": binding_id,
                "connectionRef": connection_ref,
                "providerId": provider,
                "profileRef": profile_ref,
                "tools": [
                    {
                        "toolId": tool_id,
                        "operationId": f"{provider}.list",
                        "resourceProjection": f"{provider}_safe_summary",
                        "mutating": False,
                    }
                ],
            }
        )
        catalog_items.append(
            {
                "bindingId": binding_id,
                "capabilityId": "tool.provider.v1",
                "providerId": provider,
                "connectionRef": connection_ref,
                "profileRef": profile_ref,
                "tools": [
                    {
                        "id": tool_id,
                        "name": f"Read {provider} {index}",
                        "description": "Read the already-issued scoped summary.",
                        "readOnly": True,
                        "inputSchema": {"type": "object", "additionalProperties": False},
                    }
                ],
            }
        )

    bindings_path = Path(compose["configs"]["connector-bindings"]["file"])
    catalog_path = Path(compose["configs"]["connector-tool-catalog"]["file"])
    bindings_path.write_text(json.dumps({"schemaVersion": 1, "bindings": bindings}))
    catalog_document = {"schemaVersion": 1, "items": catalog_items}
    catalog_path.write_text(json.dumps(catalog_document))
    snapshot["profiles"] = [
        {
            "profileRef": "github-profile",
            "profileAlias": "github-primary",
            "providerId": "github",
            "authStatus": "connected",
            "accessMode": "actor_owned",
            "targetScope": {"repositories": []},
        },
        {
            "profileRef": "computer-profile",
            "profileAlias": "computer-primary",
            "providerId": "computer",
            "accessMode": "workspace_operator",
            "targetScope": {"targets": {}},
        },
    ]
    snapshot["connections"] = [
        {
            "connectionRef": "github-connection",
            "providerId": "github",
            "profileRef": "github-profile",
            "profileAlias": "github-primary",
            "targetAlias": "github-read",
            "accessMode": "actor_owned",
            "readOnly": True,
            "targetScope": {"repositories": []},
        },
        {
            "connectionRef": "computer-connection",
            "providerId": "computer",
            "profileRef": "computer-profile",
            "profileAlias": "computer-primary",
            "targetAlias": "computer-read",
            "accessMode": "workspace_operator",
            "readOnly": True,
            "targetScope": {"targetAlias": "computer-read", "targetRef": "desktop"},
        },
    ]
    snapshot["bindings"] = bindings
    snapshot["catalog"] = catalog_document

    receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
        phase="provider-bootstrap",
        image_update_target="exchange",
    )

    assert receipt["verifiedProviderIds"] == ["computer", "github"]
    assert receipt["bindingCount"] == 11
    assert receipt["profileCount"] == 2
    assert receipt["connectionCount"] == 2
    assert receipt["profileRefByProvider"] == {}

    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["bindings"][0]["tools"][0]["resourceProjection"] = "other_scope"
    with pytest.raises(stage_transition.StageTransitionError, match="issued bindings differ"):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=changed_snapshot,
            compose=compose,
            phase="provider-bootstrap",
            image_update_target="exchange",
        )

    changed_catalog = copy.deepcopy(snapshot)
    changed_catalog["catalog"]["items"][0]["tools"][0]["inputSchema"] = {
        "type": "object",
        "properties": {"scope": {"type": "string"}},
    }
    with pytest.raises(stage_transition.StageTransitionError, match="live provider tool catalog"):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=changed_catalog,
            compose=compose,
            phase="provider-bootstrap",
            image_update_target="exchange",
        )


def test_exchange_readiness_requires_live_issued_profiles_bindings_and_catalog(
    tmp_path: Path,
) -> None:
    """Bind candidate scope and Navigator tools to the exact live Exchange readback."""

    snapshot, compose = _exchange_readiness_fixture(tmp_path)
    receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot, compose=compose
    )
    assert receipt["status"] == "LIVE_VERIFIED"
    assert receipt["verifiedProviderIds"] == ["azure", "gcp", "huggingface"]
    assert receipt["bindingCount"] == 3

    wrong_access_mode = copy.deepcopy(snapshot)
    next(row for row in wrong_access_mode["connections"] if row["providerId"] == "huggingface")[
        "accessMode"
    ] = "actor_owned"
    with pytest.raises(
        stage_transition.StageTransitionError, match="profile/connection access mode"
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=wrong_access_mode, compose=compose
        )

    wrong_scope = copy.deepcopy(snapshot)
    next(row for row in wrong_scope["profiles"] if row["providerId"] == "azure")["targetScope"][
        "targets"
    ]["azure-read"]["targetRef"] = "other"
    with pytest.raises(
        stage_transition.StageTransitionError, match="cloud profile scope/read-only"
    ):
        stage_transition._verify_exchange_readiness_snapshot(snapshot=wrong_scope, compose=compose)

    wrong_identity = copy.deepcopy(compose)
    wrong_exchange = json.loads(
        wrong_identity["services"]["exchange"]["environment"][
            "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"
        ]
    )
    wrong_exchange["authorizationCloud"]["profiles"]["azure-primary"]["identityRef"] = (
        "different-identity"
    )
    wrong_identity["services"]["exchange"]["environment"][
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"
    ] = json.dumps(wrong_exchange)
    with pytest.raises(
        stage_transition.StageTransitionError, match="does not join to Cloud Management"
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=snapshot, compose=wrong_identity
        )

    wrong_catalog = copy.deepcopy(snapshot)
    wrong_catalog["catalog"]["items"][0]["tools"][0]["readOnly"] = False
    with pytest.raises(stage_transition.StageTransitionError, match="live provider tool catalog"):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=wrong_catalog, compose=compose
        )


def test_live_exchange_readiness_runs_in_exact_pinned_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the official in-image command and secret-file entrypoint without shell interpolation."""

    snapshot, compose = _exchange_readiness_fixture(tmp_path)
    container_id = "a" * 64
    image_id = _image_id(77)
    captured: list[str] = []

    def checked(argv: list[str], **_kwargs: Any) -> str:
        captured.extend(argv)
        return json.dumps(snapshot, separators=(",", ":"))

    monkeypatch.setattr(stage_transition.runtime_update, "_checked_stdout", checked)
    receipt = stage_transition._read_live_exchange_readiness(
        plan={
            "candidateServices": {"exchange": {"candidate": {"imageId": image_id}}},
            "candidateConfigDir": str(tmp_path),
            "policy": {"dockerPrefix": ["docker"]},
        },
        compose=compose,
        container_id=container_id,
        image_id=image_id,
        docker_prefix=["docker"],
    )

    assert captured[:5] == [
        "docker",
        "exec",
        container_id,
        stage_transition.EXCHANGE_SECRET_ENV_ENTRYPOINT,
        stage_transition.EXCHANGE_READINESS_EXECUTABLE,
    ]
    assert "connector-readiness" in captured
    assert stage_transition.EXCHANGE_INTEGRATION_DATABASE in captured
    assert receipt["exchangeContainerId"] == container_id
    assert receipt["exchangeImageId"] == image_id
    assert receipt["status"] == "LIVE_VERIFIED"


def test_compose_stage_target_is_closed_and_never_pulls_or_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the shared runtime mutation wrapper with a fixed service order and no image I/O."""

    captured: list[str] = []
    monkeypatch.setattr(
        runtime_update,
        "_run_command",
        lambda argv, **_kwargs: captured.extend(argv) or SimpleNamespace(returncode=0),
    )
    runtime_update._compose_up_stage_transition(
        docker_prefix=["docker"],
        project="cyrene-teams-cloud",
        compose_files=[Path("compose.yaml")],
        env_files=[Path("runtime.env")],
        service="navigator-persistence",
        timeout_seconds=60,
    )
    assert captured[-8:] == [
        "up",
        "--detach",
        "--no-deps",
        "--pull",
        "never",
        "--no-build",
        "--force-recreate",
        "navigator-persistence",
    ]
    profiles = [
        captured[index + 1]
        for index, argument in enumerate(captured[:-1])
        if argument == "--profile"
    ]
    assert profiles == list(runtime_update.preflight.ACTIVE_COMPOSE_PROFILES)
    assert "directory-bootstrap" not in profiles
    assert captured[captured.index("up") + 1 :] == [
        "--detach",
        "--no-deps",
        "--pull",
        "never",
        "--no-build",
        "--force-recreate",
        "navigator-persistence",
    ]
    with pytest.raises(runtime_update.RuntimeUpdateError, match="outside the formal"):
        runtime_update._compose_up_stage_transition(
            docker_prefix=["docker"],
            project="cyrene-teams-cloud",
            compose_files=[Path("compose.yaml")],
            env_files=[Path("runtime.env")],
            service="second-navigator",
            timeout_seconds=60,
        )


def test_hf_failure_rolls_back_hf_but_never_downgrades_migrated_account_center(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep Account Center's candidate image after a later HF failure may follow schema v8."""

    account_center_id = "a" * 64
    huggingface_id = "b" * 64
    account_center_image = _image_id(20)
    huggingface_image = _image_id(21)
    active_lock = _lock(
        [
            ("navigator-persistence", "persistence:v2", 7),
            ("dsh-executor", "navigator:v1", 2),
            ("account-center", "account-center:v1", 3),
        ]
    )
    candidate_compose = {
        "name": "cyrene-teams-cloud",
        "services": {
            "navigator-persistence": _mount_service("persistence:v2"),
            "navigator": _mount_service("navigator:v1", executor=True),
            "account-center": _service("account-center:v2"),
            "huggingface": _service("huggingface:v2"),
        },
    }
    plan = {
        "project": "cyrene-teams-cloud",
        "targets": ["account-center", "huggingface"],
        "activeInputs": {"imageLock": {"path": "/private/active-lock.json"}},
        "candidateInputs": {
            "composeFiles": [{"path": "/private/candidate-compose.yaml"}],
            "envFiles": [{"path": "/private/candidate.env"}],
        },
        "candidateServices": {
            "account-center": {
                "active": {"imageRef": "account-center:v1", "imageId": _image_id(3)},
                "candidate": {"imageRef": "account-center:v2", "imageId": account_center_image},
                "added": False,
            },
            "huggingface": {
                "candidate": {"imageRef": "huggingface:v2", "imageId": huggingface_image},
                "added": True,
            },
        },
    }
    journal = {
        "state": "RUNNING",
        "lastErrorCode": None,
        "database": {"restorePerformed": False},
        "targetProgress": {
            "account-center": {
                "phase": "APPLIED",
                "oldContainerId": "c" * 64,
                "candidateContainerId": account_center_id,
                "added": False,
                "applyIntent": False,
                "rollbackIntent": False,
            },
            "huggingface": {
                "phase": "CONTAINER_READBACK",
                "oldContainerId": None,
                "candidateContainerId": huggingface_id,
                "added": True,
                "applyIntent": True,
                "rollbackIntent": False,
            },
        },
    }
    container_presence = iter([True, False])
    commands: list[list[str]] = []
    journal_phases: list[str] = []
    inspection = [
        {
            "Id": huggingface_id,
            "Image": huggingface_image,
            "Config": {
                "Image": "huggingface:v2",
                "Labels": {
                    "com.docker.compose.project": "cyrene-teams-cloud",
                    "com.docker.compose.service": "huggingface",
                },
            },
        }
    ]

    monkeypatch.setattr(runtime_update, "_load_lock", lambda _path: active_lock)
    monkeypatch.setattr(stage_transition, "_lock_image_ids", lambda _lock: {})
    monkeypatch.setattr(
        stage_transition.preflight, "_compose_config", lambda *_args: candidate_compose
    )
    monkeypatch.setattr(
        stage_transition,
        "_runtime_service_inventory",
        lambda _compose: {"huggingface": candidate_compose["services"]["huggingface"]},
    )
    monkeypatch.setattr(
        stage_transition,
        "_container_exists",
        lambda _container_id, _prefix: next(container_presence),
    )
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: json.dumps(inspection),
    )

    def remove_huggingface(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runtime_update, "_run_command", remove_huggingface)
    monkeypatch.setattr(
        stage_transition,
        "_compose_target_update",
        lambda **_kwargs: pytest.fail("rollback must not recreate the old Account Center image"),
    )

    def write_journal(
        _path: Path, document: dict[str, Any], phase: str, *, state: str | None = None
    ) -> None:
        document["phase"] = phase
        if state is not None:
            document["state"] = state
        journal_phases.append(phase)

    monkeypatch.setattr(stage_transition, "_write_journal", write_journal)

    with pytest.raises(
        stage_transition.StageTransitionError, match="may have migrated durable SQLite"
    ):
        stage_transition._rollback_known_mutations(
            plan=plan,
            active_compose={"services": {}},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )

    assert commands == [["docker", "rm", "-f", huggingface_id]]
    assert journal["targetProgress"]["huggingface"]["phase"] == "ROLLED_BACK"
    assert journal["targetProgress"]["account-center"]["phase"] == "APPLIED"
    assert journal["lastErrorCode"] == stage_transition.ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR
    assert journal["state"] == "RECOVERY_REQUIRED"
    assert journal["database"]["restorePerformed"] is False
    assert "ACCOUNT_CENTER_SCHEMA_DOWNGRADE_BLOCKED" in journal_phases


def test_account_center_startup_health_failure_keeps_apply_intent_and_blocks_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed initial AC health wait cannot assume that its DB migration did not commit."""

    account_center_id = "d" * 64
    account_center_image = _image_id(22)
    candidate_compose = {
        "name": "cyrene-teams-cloud",
        "services": {
            "navigator-persistence": _mount_service("persistence:v2"),
            "navigator": _mount_service("navigator:v1", executor=True),
            "account-center": _service("account-center:v2"),
        },
    }
    plan = {
        "project": "cyrene-teams-cloud",
        "targets": ["account-center"],
        "activeInputs": {"imageLock": {"path": "/private/active-lock.json"}},
        "candidateInputs": {"composeFiles": [], "envFiles": []},
        "candidateServices": {
            "account-center": {
                "active": {"imageRef": "account-center:v1", "imageId": _image_id(3)},
                "candidate": {"imageRef": "account-center:v2", "imageId": account_center_image},
                "added": False,
            }
        },
        "policy": {"healthTimeoutSeconds": 30},
    }
    journal = {
        "state": "RUNNING",
        "lastErrorCode": None,
        "database": {"restorePerformed": False},
        "targetProgress": {
            "account-center": {
                "phase": "PREPARED",
                "oldContainerId": "c" * 64,
                "candidateContainerId": None,
                "added": False,
                "applyIntent": False,
                "rollbackIntent": False,
            }
        },
    }
    compose_updates: list[bool] = []
    journal_phases: list[str] = []
    observed_candidate = {
        "containerId": account_center_id,
        "imageRef": "account-center:v2",
        "imageId": account_center_image,
        "health": "starting",
        "networkNames": ["account-center-private"],
    }

    monkeypatch.setattr(
        stage_transition,
        "_runtime_service_inventory",
        lambda _compose: {"account-center": candidate_compose["services"]["account-center"]},
    )

    def compose_update(**kwargs: Any) -> SimpleNamespace:
        compose_updates.append(kwargs["use_candidate"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(stage_transition, "_compose_target_update", compose_update)
    monkeypatch.setattr(
        stage_transition, "_inspect_exact_service", lambda **_kwargs: observed_candidate
    )
    monkeypatch.setattr(
        stage_transition,
        "_wait_for_target_service",
        lambda **_kwargs: (_ for _ in ()).throw(
            stage_transition.StageTransitionError("account-center candidate health failed")
        ),
    )

    def write_journal(
        _path: Path, document: dict[str, Any], phase: str, *, state: str | None = None
    ) -> None:
        document["phase"] = phase
        if state is not None:
            document["state"] = state
        journal_phases.append(phase)

    monkeypatch.setattr(stage_transition, "_write_journal", write_journal)

    with pytest.raises(stage_transition.StageTransitionError, match="candidate health failed"):
        stage_transition._update_target(
            plan=plan,
            candidate_compose=candidate_compose,
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
            service_name="account-center",
        )

    assert journal["targetProgress"]["account-center"]["applyIntent"] is True
    assert journal["targetProgress"]["account-center"]["candidateContainerId"] == account_center_id

    monkeypatch.setattr(runtime_update, "_load_lock", lambda _path: {})
    monkeypatch.setattr(stage_transition, "_lock_image_ids", lambda _lock: {})
    with pytest.raises(
        stage_transition.StageTransitionError, match="may have migrated durable SQLite"
    ):
        stage_transition._rollback_known_mutations(
            plan=plan,
            active_compose={"services": {}},
            journal=journal,
            journal_path=tmp_path / "operation.json",
            docker_prefix=["docker"],
        )

    assert compose_updates == [True]
    assert journal["lastErrorCode"] == stage_transition.ACCOUNT_CENTER_DOWNGRADE_BLOCKED_ERROR
    assert journal["state"] == "RECOVERY_REQUIRED"
    assert journal["database"]["restorePerformed"] is False
    assert "ACCOUNT_CENTER_SCHEMA_DOWNGRADE_BLOCKED" in journal_phases


def test_same_account_center_image_is_not_blocked_as_a_schema_downgrade() -> None:
    """A config-only rollback to the same immutable image does not downgrade the DB schema."""

    image = {"imageRef": "account-center:current", "imageId": _image_id(23)}
    assert not stage_transition._account_center_image_downgrade_may_be_unsafe(
        plan={"candidateServices": {"account-center": {"active": image, "candidate": image}}},
        progress={
            "phase": "APPLIED",
            "applyIntent": False,
            "rollbackIntent": False,
            "candidateContainerId": "e" * 64,
        },
    )
