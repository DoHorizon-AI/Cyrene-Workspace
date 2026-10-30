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
import sqlite3
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
stage_transition = importlib.import_module("stage_transition")
runtime_update = importlib.import_module("runtime_update")
preflight = importlib.import_module("preflight")


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
        "composeCgroupMemoryLimitMiB": 3520,
        "composeCgroupCpuLimit": 1.90,
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
    monkeypatch.setattr(
        stage_transition,
        "_validate_active_candidate_configs",
        lambda *_args, **_kwargs: None,
    )
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
    tmp_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build the canonical runtime inventory with one candidate image changed."""

    active, _unused_candidate, active_lock, _unused_lock = _listener_repair_pair(tmp_path)
    lock_service = runtime_update.LOCK_SERVICE_FOR_TARGET[target]
    candidate = copy.deepcopy(active)
    candidate_lock = copy.deepcopy(active_lock)
    candidate_ref = f"{target}:image-update"
    candidate["services"][target]["image"] = candidate_ref
    row = next(item for item in candidate_lock["images"] if item["service"] == lock_service)
    row["image"] = candidate_ref
    row["imageId"] = _image_id(50 if target == "exchange" else 51)
    return active, candidate, active_lock, candidate_lock


def _merge_compose_fixture_value(base: Any, override: Any) -> Any:
    """Apply the Compose map/list override shape needed by the canonical test inputs."""

    if isinstance(base, dict) and isinstance(override, dict):
        merged = copy.deepcopy(base)
        for key, value in override.items():
            merged[key] = (
                _merge_compose_fixture_value(merged[key], value)
                if key in merged
                else copy.deepcopy(value)
            )
        return merged
    if isinstance(base, list) and isinstance(override, list):
        return copy.deepcopy(base) + copy.deepcopy(override)
    return copy.deepcopy(override)


def _listener_repair_pair(
    tmp_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Render the selected canonical Compose overlays into the fixed 14-service shape.

    中文：服务键、profile 和实际挂载引用来自正式 Compose 文件，不从六个更新目标合成。
    """

    deployment_root = Path(__file__).resolve().parents[1]
    compose_names = (
        "compose.yaml",
        "compose.computer-runtime.yaml",
        "compose.codebuddy-acp.yaml",
        "compose.huggingface.yaml",
        "compose.cloud-phase2.yaml",
        "compose.credential-notifications.yaml",
    )
    active: dict[str, Any] = {
        "services": {},
        "networks": {},
        "volumes": {},
        "configs": {},
        "secrets": {},
    }
    for compose_name in compose_names:
        document = yaml.safe_load((deployment_root / compose_name).read_text(encoding="utf-8"))
        for field in ("services", "networks", "volumes", "configs", "secrets"):
            rows = document.get(field)
            if isinstance(rows, dict):
                active[field] = _merge_compose_fixture_value(active[field], rows)
        if document.get("name"):
            active["name"] = document["name"]

    active["name"] = "cyrene-teams-cloud"
    runtime_services = stage_transition._runtime_service_inventory(active)
    assert set(runtime_services) == stage_transition.RUNTIME_SERVICE_SET

    for field in ("configs", "secrets"):
        definitions = active[field]
        for name, definition in definitions.items():
            source = tmp_path / f"{field}-{name}"
            source.write_text(f"fixture:{field}:{name}", encoding="utf-8")
            definition["file"] = str(source)

    lock_rows: list[tuple[str, str, int]] = []
    for index, service_name in enumerate(stage_transition.RUNTIME_SERVICE_ORDER, start=1):
        image = f"example.invalid/{service_name}:fixed"
        active["services"][service_name]["image"] = image
        lock_rows.append(
            (runtime_update.LOCK_SERVICE_FOR_TARGET.get(service_name, service_name), image, index)
        )

    persistence = active["services"]["navigator-persistence"]
    persistence["environment"]["NAVIGATOR_DATABASE_PATH"] = stage_transition.DATABASE_PATH
    persistence["volumes"] = [
        {
            "type": "volume",
            "source": "navigator-sessions",
            "target": stage_transition.DATABASE_VOLUME_TARGET,
        }
    ]
    navigator = active["services"]["navigator"]
    navigator["volumes"] = [
        {
            "type": "bind",
            "source": "/private/codebuddy",
            "target": stage_transition.CODEBUDDY_HOME_TARGET,
        },
        {
            "type": "volume",
            "source": "navigator-sessions",
            "target": stage_transition.DATABASE_VOLUME_TARGET,
        },
    ]
    active["services"]["exchange"]["environment"][stage_transition.EXCHANGE_CLOUD_RUNNER_ENV] = (
        "false"
    )
    active["services"]["huggingface"]["command"] = list(
        stage_transition.PROVIDER_DEFAULT_COMMANDS["huggingface"]
    )
    active["services"]["cloud-management-phase2"]["command"] = list(
        stage_transition.PROVIDER_DEFAULT_COMMANDS["cloud-management-phase2"]
    )

    candidate = copy.deepcopy(active)
    for service_name in stage_transition.PROVIDER_LISTENER_REPAIR_TARGETS:
        candidate["services"][service_name]["command"] = list(
            stage_transition.PROVIDER_WILDCARD_COMMANDS[service_name]
        )
    lock = _lock(lock_rows)
    return active, candidate, lock, copy.deepcopy(lock)


def _full_cloud_image_update(
    tmp_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build a Cloud image pair over the canonical 14-service Compose inventory."""

    active, _listener_candidate, active_lock, _candidate_lock = _listener_repair_pair(tmp_path)
    target = "cloud-management-phase2"
    target_lock_service = runtime_update.LOCK_SERVICE_FOR_TARGET[target]
    active_lock = copy.deepcopy(active_lock)

    candidate = copy.deepcopy(active)
    candidate_lock = copy.deepcopy(active_lock)
    candidate_ref = "example.invalid/cloud-management-phase2:issued-update"
    candidate["services"][target]["image"] = candidate_ref
    candidate_row = next(
        row for row in candidate_lock["images"] if row["service"] == target_lock_service
    )
    candidate_row["image"] = candidate_ref
    candidate_row["imageId"] = _image_id(100)
    return active, candidate, active_lock, candidate_lock


def _office_personal_route_services() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return active and candidate service fragments for the five-actor route delta."""

    owner = "d83aeb27-ff11-46d7-b0f7-af8da8d5b8ec"
    actor_secrets = stage_transition.OFFICE_PERSONAL_ROUTE_SECRET_NAMES
    route_map = {
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN": "/run/secrets/teams-private-bridge-token",
        **{
            f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}": f"/run/secrets/work-actor-token-{index:02d}"
            for index in range(1, 6)
        },
    }
    map_text = lambda mapping: ",".join(f"{key}={value}" for key, value in mapping.items())
    auth_mount = {
        "type": "bind",
        "source": "/active/private-config",
        "target": "/run/teams-authorization",
        "read_only": True,
    }
    active = {
        "navigator": {
            "image": "navigator:old",
            "environment": {
                "CYRENE_SECRET_FILE_MAP": map_text(
                    {"CYRENE_SESSION_TOKEN": "/run/secrets/navigator-session-token"}
                ),
                "CYRENE_WORKFLOWS_ENABLED": "false",
            },
            "secrets": [],
            "volumes": [auth_mount],
        },
        "navigator-persistence": {
            "image": "persistence:old",
            "environment": {
                "CYRENE_SECRET_FILE_MAP": map_text(
                    {"NAVIGATOR_SESSION_TOKEN": "/run/secrets/navigator-session-token"}
                )
            },
            "secrets": [],
            "volumes": [],
        },
        "teams-ingress": {"image": "ingress:old", "environment": {}},
        "teams-provider": {
            "image": "teams:old",
            "environment": {"CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps({})},
        },
        "directory-operator": {
            "environment": {"CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID": owner},
            "volumes": [
                {
                    "type": "bind",
                    "source": "/active/private-config",
                    "target": "/run/cyrene-config",
                }
            ],
        },
    }
    candidate = copy.deepcopy(active)
    candidate["navigator"]["image"] = "navigator:new"
    candidate["navigator"]["environment"].update(
        {
            "CYRENE_WORK_ACTOR_TOKEN_MAP_FILE": "/run/teams-authorization/work-actor-token-map.json",
            "CYRENE_AUTOMATION_SOURCE_BINDINGS_FILE": "/run/teams-authorization/automation-source-bindings.json",
            "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED": "true",
            "CYRENE_TEAMS_MODEL_ACTOR_SUBJECT": owner,
            "CYRENE_SECRET_FILE_MAP": map_text(
                {
                    "CYRENE_SESSION_TOKEN": "/run/secrets/navigator-session-token",
                    **route_map,
                }
            ),
        }
    )
    candidate["navigator"]["secrets"] = [
        {"source": name, "target": name} for name in sorted(actor_secrets)
    ]
    candidate["navigator"]["volumes"].append(
        {
            "type": "bind",
            "source": "/private/automation-health",
            "target": "/run/cyrene/automation-health",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    )
    candidate["navigator-persistence"]["image"] = "persistence:new"
    candidate["navigator-persistence"]["environment"].update(
        {
            "TEAMS_TASK_ACTOR_SUBJECTS_FILE": "/run/teams-authorization/teams-actor-subjects.json",
            "CYRENE_SECRET_FILE_MAP": map_text(
                {"NAVIGATOR_SESSION_TOKEN": "/run/secrets/navigator-session-token", **route_map}
            ),
        }
    )
    candidate["navigator-persistence"]["secrets"] = [
        {"source": name, "target": name} for name in sorted(actor_secrets)
    ]
    candidate["navigator-persistence"]["volumes"].append(
        {
            "type": "bind",
            "source": "/candidate/private-config",
            "target": "/run/teams-authorization",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    )
    candidate["navigator"]["volumes"][0]["source"] = "/candidate/private-config"
    candidate["teams-ingress"]["image"] = "ingress:new"
    candidate["teams-ingress"]["environment"]["TEAMS_PERSONAL_CHAT_ENABLED"] = "true"
    candidate["teams-provider"]["image"] = "teams:new"
    candidate["teams-provider"]["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(
        {"personal_chat_enabled": True}
    )
    candidate["directory-operator"]["volumes"][0]["source"] = "/candidate/private-config"
    return active, candidate


def test_office_graph_bootstrap_accepts_only_the_owner_model_personal_route() -> None:
    active, candidate = _office_personal_route_services()

    stage_transition._validate_office_graph_personal_route_compose_delta(active, candidate)

    active, candidate = _office_personal_route_services()
    changed = copy.deepcopy(candidate)
    changed["navigator"]["environment"]["CYRENE_TEAMS_MODEL_ACTOR_SUBJECT"] = (
        "22222222-2222-4222-8222-222222222222"
    )
    with pytest.raises(stage_transition.StageTransitionError, match="owner"):
        stage_transition._validate_office_graph_personal_route_compose_delta(
            copy.deepcopy(active), changed
        )

    active, candidate = _office_personal_route_services()
    candidate["navigator"]["environment"]["CYRENE_WORKFLOWS_ENABLED"] = "true"
    with pytest.raises(stage_transition.StageTransitionError, match="Workflows disabled"):
        stage_transition._validate_office_graph_personal_route_compose_delta(active, candidate)


def test_office_graph_bootstrap_principal_delta_adds_only_private_chat_policy(
    tmp_path: Path,
) -> None:
    active_dir = tmp_path / "active-config"
    candidate_dir = tmp_path / "candidate-config"
    active_dir.mkdir()
    candidate_dir.mkdir()
    owner = "11111111-1111-4111-8111-111111111111"
    actors = [
        owner,
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
        "55555555-5555-4555-8555-555555555555",
    ]
    workspace = "77777777-7777-4777-8777-777777777777"
    organization = "66666666-6666-4666-8666-666666666666"
    tenant = "88888888-8888-4888-8888-888888888888"
    bot = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    active_file = active_dir / stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
    candidate_file = candidate_dir / stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
    session = {
        "token_env": "NAVIGATOR_SESSION_TOKEN",
        "actor_id": owner,
        "workspace_ids": [workspace],
        "organization_id": organization,
        "can_takeover": True,
        "can_write_harness": False,
        "can_publish_credential_health": False,
        "can_bind_teams_private_conversations": False,
    }
    old_document = {
        "principals": [session],
        "product_directory": [],
        "product_service_credentials": [],
    }
    active_file.write_text(json.dumps(old_document), encoding="utf-8")
    projection = {
        "schemaVersion": 1,
        "workspaceId": workspace,
        "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "subjects": actors.copy(),
    }
    (active_dir / "teams-actor-subjects.json").write_text(json.dumps(projection), encoding="utf-8")
    (candidate_dir / "teams-actor-subjects.json").write_text(
        json.dumps(projection), encoding="utf-8"
    )
    token_envs = [f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}" for index in range(1, 6)]
    actor_map = {
        "schemaVersion": 1,
        "workspaceId": workspace,
        "actors": [
            {"actorAadObjectId": actor, "tokenEnv": token_env}
            for actor, token_env in zip(actors, token_envs, strict=True)
        ],
    }
    (candidate_dir / "work-actor-token-map.json").write_text(
        json.dumps(actor_map), encoding="utf-8"
    )
    (candidate_dir / "automation-source-bindings.json").write_text(
        json.dumps({"schemaVersion": 1, "targets": []}), encoding="utf-8"
    )
    policy = {
        "actor_subjects_file_env": "TEAMS_TASK_ACTOR_SUBJECTS_FILE",
        "workspace_id": workspace,
        "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
        "tenant_id": tenant,
        "bot_app_id": bot,
    }
    writers = [
        {
            "token_env": token_env,
            "actor_id": actor,
            "workspace_ids": [workspace],
            "organization_id": organization,
            "can_takeover": True,
            "actor_private_scope": True,
            "can_write_harness": True,
            "can_publish_credential_health": False,
            "can_bind_teams_private_conversations": False,
        }
        for actor, token_env in zip(actors, token_envs, strict=True)
    ]
    bridge = {
        "token_env": "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
        "actor_id": "teams-private-bridge",
        "workspace_ids": [workspace],
        "organization_id": organization,
        "can_takeover": False,
        "actor_private_scope": False,
        "can_write_harness": False,
        "can_publish_credential_health": False,
        "can_bind_teams_private_conversations": True,
    }
    candidate_file.write_text(
        json.dumps(
            {
                **old_document,
                "principals": [session, *writers, bridge],
                "teams_private_conversations": policy,
            }
        ),
        encoding="utf-8",
    )
    auth_mount = {
        "type": "bind",
        "source": str(active_dir),
        "target": "/run/teams-authorization",
        "read_only": True,
    }
    candidate_auth_mount = {**auth_mount, "source": str(candidate_dir)}
    teams_profile = {
        "connectionRef": "teams-connection",
        "directPlugin": {"connectionRef": "grpcs://teams-provider:7443", "transport": "mtls"},
        "botAppId": bot,
        "expectedIssuer": policy["issuer"],
        "workspaceId": workspace,
    }
    active = {
        "configs": {"navigator-principal": {"file": str(active_file)}},
        "services": {
            "navigator-persistence": {
                "configs": [
                    {
                        "source": "navigator-principal",
                        "target": stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_TARGET,
                    }
                ]
            },
            "navigator": {
                "configs": [],
                "volumes": [auth_mount],
                "environment": {
                    "CYRENE_WORKSPACE_ID": workspace,
                    "TEAMS_TENANT_ID": tenant,
                    "TEAMS_BOT_APP_ID": bot,
                },
            },
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                        {"connectorHost": {"teamsMessages": teams_profile}}
                    )
                }
            },
            "directory-operator": {
                "environment": {
                    "CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID": owner,
                    "CYRENE_ORGANIZATION_ID": organization,
                }
            },
        },
    }
    candidate = copy.deepcopy(active)
    candidate["configs"]["navigator-principal"]["file"] = str(candidate_file)
    candidate["services"]["navigator"]["volumes"] = [candidate_auth_mount]
    candidate_profile = {**teams_profile, "personalActorSubjects": actors.copy()}
    candidate["services"]["exchange"]["environment"][
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"
    ] = json.dumps({"connectorHost": {"teamsMessages": candidate_profile}})
    stage_transition._validate_office_graph_private_principal_delta(active, candidate)

    candidate_document = json.loads(candidate_file.read_text(encoding="utf-8"))
    changed = copy.deepcopy(candidate_document)
    changed["principals"][1]["can_publish_credential_health"] = True
    candidate_file.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(stage_transition.StageTransitionError, match="writer principals"):
        stage_transition._validate_office_graph_private_principal_delta(active, candidate)
    candidate_file.write_text(json.dumps(candidate_document), encoding="utf-8")

    (candidate_dir / "unreviewed-config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(stage_transition.StageTransitionError, match="add only"):
        stage_transition._validate_office_graph_private_principal_delta(active, candidate)
    (candidate_dir / "unreviewed-config.json").unlink()

    (candidate_dir / "automation-source-bindings.json").write_text(
        json.dumps({"schemaVersion": 1, "targets": [{"kind": "host_health"}]}),
        encoding="utf-8",
    )
    with pytest.raises(stage_transition.StageTransitionError, match="Directory projection"):
        stage_transition._validate_office_graph_private_principal_delta(active, candidate)


def test_office_graph_activation_grants_health_publishing_to_owner_session_only(
    tmp_path: Path,
) -> None:
    active_dir = tmp_path / "active-config"
    candidate_dir = tmp_path / "candidate-config"
    active_dir.mkdir()
    candidate_dir.mkdir()
    owner = "11111111-1111-4111-8111-111111111111"
    principal_rows = [
        {
            "token_env": "NAVIGATOR_SESSION_TOKEN",
            "actor_id": owner,
            "can_publish_credential_health": False,
        }
    ]
    principal_rows.extend(
        {
            "token_env": f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}",
            "actor_id": f"{index:08d}-1111-4111-8111-{index:012d}",
            "can_publish_credential_health": False,
        }
        for index in range(1, 6)
    )
    principal_rows.append(
        {
            "token_env": "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
            "actor_id": "teams-private-bridge",
            "can_publish_credential_health": False,
        }
    )
    policy = {"actor_subjects_file_env": "TEAMS_TASK_ACTOR_SUBJECTS_FILE"}
    active_file = active_dir / stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
    candidate_file = candidate_dir / stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
    active_file.write_text(
        json.dumps({"principals": principal_rows, "teams_private_conversations": policy}),
        encoding="utf-8",
    )
    candidate_rows = copy.deepcopy(principal_rows)
    candidate_rows[0]["can_publish_credential_health"] = True
    candidate_file.write_text(
        json.dumps({"principals": candidate_rows, "teams_private_conversations": policy}),
        encoding="utf-8",
    )
    alias = "navigator-principal"
    active = {
        "configs": {alias: {"file": str(active_file)}},
        "services": {
            "navigator-persistence": {
                "configs": [
                    {
                        "source": alias,
                        "target": stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_TARGET,
                    }
                ]
            },
            "navigator": {"configs": []},
            "directory-operator": {
                "environment": {"CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID": owner}
            },
        },
    }
    candidate = copy.deepcopy(active)
    candidate["configs"][alias]["file"] = str(candidate_file)

    stage_transition._validate_office_graph_owner_health_principal_delta(
        active_config_dir=active_dir,
        candidate_config_dir=candidate_dir,
        active=active,
        candidate=candidate,
    )

    candidate_rows[1]["can_publish_credential_health"] = True
    candidate_file.write_text(
        json.dumps({"principals": candidate_rows, "teams_private_conversations": policy}),
        encoding="utf-8",
    )
    with pytest.raises(stage_transition.StageTransitionError, match="beyond owner health"):
        stage_transition._validate_office_graph_owner_health_principal_delta(
            active_config_dir=active_dir,
            candidate_config_dir=candidate_dir,
            active=active,
            candidate=candidate,
        )


def test_office_graph_exchange_delta_allows_only_private_subject_projection() -> None:
    active = {
        "connectorHost": {"teamsMessages": {"connectionRef": "teams-connection"}},
        "accountCenter": {},
        "hostSecretRefs": {},
    }
    candidate = copy.deepcopy(active)
    candidate["accountCenter"] = {
        "callbackCallerId": "microsoft-entra-callback",
        "callbackSigningKeyRef": "account-center/caller-hmac-keys/microsoft-graph-callback",
    }
    candidate["hostSecretRefs"] = {
        "account-center/caller-hmac-keys/microsoft-graph-callback": (
            "CYRENE_ACCOUNT_CENTER_MICROSOFT_GRAPH_CALLBACK_HMAC_KEY"
        )
    }
    candidate["connectorHost"]["teamsMessages"]["personalActorSubjects"] = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
        "55555555-5555-4555-8555-555555555555",
    ]

    stage_transition._validate_office_graph_exchange_config_delta(active, candidate)

    candidate["connectorHost"]["teamsMessages"]["personalActorSubjects"] = [
        "11111111-1111-4111-8111-111111111111"
    ]
    with pytest.raises(stage_transition.StageTransitionError, match="five pilot actor subjects"):
        stage_transition._validate_office_graph_exchange_config_delta(active, candidate)


def test_office_graph_account_center_only_appends_graph_lease_role() -> None:
    callback_ref = "account-center/caller-hmac-keys/microsoft-graph-callback"
    provider_ref = "account-center/caller-hmac-keys/microsoft-graph-provider"
    client_ref = "microsoft-graph/client-secret"
    active = {
        "exchange_caller_id": "exchange-service",
        "caller_key_refs": {"exchange-service": "account/signing"},
        "hostSecretRefs": {"account/signing": "CYRENE_ACCOUNT_CENTER_EXCHANGE_HMAC_KEY"},
        "method_callers": {
            "redeem_provider_credential_lease": [
                "github-issues-service",
                "huggingface-provider",
            ]
        },
    }
    candidate = copy.deepcopy(active)
    candidate.update(
        {
            "microsoft_graph_client_id": "11111111-1111-4111-8111-111111111111",
            "microsoft_graph_client_secret_ref": client_ref,
            "microsoft_graph_callback_uri": "http://localhost:52191/oauth/callback",
            "microsoft_graph_callback_caller_id": "microsoft-entra-callback",
            "microsoft_graph_provider_caller_id": "microsoft-graph-provider",
        }
    )
    candidate["caller_key_refs"].update(
        {
            "microsoft-entra-callback": callback_ref,
            "microsoft-graph-provider": provider_ref,
        }
    )
    candidate["hostSecretRefs"].update(
        {
            callback_ref: "CYRENE_ACCOUNT_CENTER_MICROSOFT_GRAPH_CALLBACK_HMAC_KEY",
            provider_ref: "CYRENE_ACCOUNT_CENTER_GRAPH_PROVIDER_HMAC_KEY",
            client_ref: "CYRENE_ACCOUNT_CENTER_MICROSOFT_GRAPH_CLIENT_SECRET",
        }
    )
    candidate["method_callers"].update(
        {
            "begin_microsoft_graph_link": ["exchange-service"],
            "complete_microsoft_graph_link": ["microsoft-entra-callback"],
            "begin_microsoft_graph_scope_upgrade": ["exchange-service"],
            "complete_microsoft_graph_scope_upgrade": ["microsoft-entra-callback"],
            "update_microsoft_graph_v2_scope": ["exchange-service"],
            "list_provider_profiles": ["exchange-service", "microsoft-graph-provider"],
            "redeem_provider_credential_lease": [
                "github-issues-service",
                "huggingface-provider",
                "microsoft-graph-provider",
            ],
        }
    )

    stage_transition._validate_office_graph_account_center_config_delta(active, candidate)

    candidate["method_callers"]["redeem_provider_credential_lease"] = [
        "github-issues-service",
        "huggingface-provider",
        "microsoft-entra-callback",
        "microsoft-graph-provider",
    ]
    with pytest.raises(stage_transition.StageTransitionError, match="method callers"):
        stage_transition._validate_office_graph_account_center_config_delta(active, candidate)


def test_exchange_graph_readiness_uses_unix_expiry_metadata() -> None:
    profile = {
        "profileRef": "opaque-profile",
        "profileAlias": "microsoft-graph-office",
        "providerId": "microsoft_graph",
        "authStatus": "healthy",
        "accessMode": "actor_owned",
        "targetScope": {"schemaVersion": 2},
        "authMode": "delegated_oauth",
        "credentialRevision": 1,
        "externalSubject": "11111111-1111-4111-8111-111111111111:22222222-2222-4222-8222-222222222222",
        "scopeSchemaVersion": 2,
        "enabledFeatures": ["planner_read"],
        "requiredOAuthScopes": ["Tasks.Read"],
        "targetRefCount": 1,
        "credentialExpiresAt": 1_800_000_000,
        "refreshExpiresAt": None,
        "reauthRequired": False,
        "reauthReasonCode": None,
    }
    connection = {
        "connectionRef": "opaque-connection",
        "providerId": "microsoft_graph",
        "profileRef": "opaque-profile",
        "profileAlias": "microsoft-graph-office",
        "accessMode": "actor_owned",
        "readOnly": True,
        "targetAlias": None,
    }
    snapshot = {"profiles": [profile], "connections": [connection]}

    stage_transition._assert_exchange_readiness_safe_shape(snapshot)

    profile["credentialExpiresAt"] = "2027-01-15T00:00:00Z"
    with pytest.raises(
        stage_transition.StageTransitionError, match="profile metadata is malformed"
    ):
        stage_transition._assert_exchange_readiness_safe_shape(snapshot)


def _full_github_image_update(
    tmp_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build a GitHub provider image pair over the canonical 14-service inventory."""

    active, _listener_candidate, active_lock, _candidate_lock = _listener_repair_pair(tmp_path)
    target = "github-issues"
    target_lock_service = runtime_update.LOCK_SERVICE_FOR_TARGET[target]
    active_lock = copy.deepcopy(active_lock)

    candidate = copy.deepcopy(active)
    candidate_lock = copy.deepcopy(active_lock)
    candidate_ref = "example.invalid/github-issues:issued-update"
    candidate["services"][target]["image"] = candidate_ref
    candidate_row = next(
        row for row in candidate_lock["images"] if row["service"] == target_lock_service
    )
    candidate_row["image"] = candidate_ref
    candidate_row["imageId"] = _image_id(101)
    return active, candidate, active_lock, candidate_lock


def _active_memory_update(
    tmp_path: Path, target: str = "teams-ingress"
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build an exact 14-service Compose pair with one no-swap memory increase."""

    active, _unused_candidate, active_lock, _unused_lock = _listener_repair_pair(tmp_path)
    candidate = copy.deepcopy(active)
    candidate_service = candidate["services"][target]
    candidate_service["mem_limit"] = "256m"
    candidate_service["memswap_limit"] = "256m"
    return active, candidate, active_lock


@pytest.mark.parametrize(
    ("target", "phase"),
    [
        ("exchange", "issued-connector-activation"),
        ("huggingface", "issued-connector-activation"),
        ("navigator", "issued-connector-activation"),
        ("navigator-persistence", "issued-connector-activation"),
        ("cloud-management-phase2", "provider-bootstrap"),
    ],
)
def test_image_update_targets_exactly_one_service_and_keeps_existing_phase(
    target: str,
    phase: str,
    tmp_path: Path,
) -> None:
    """Update one built image while preserving every service/configuration in the phase.

    中文：单目标镜像更新保留现有拓扑、配置与其余镜像。
    """

    active, candidate, active_lock, candidate_lock = _active_image_update(target, tmp_path)

    targets, images = stage_transition._validate_phase_topology(
        phase=phase,
        image_update_target=target,
        active=active,
        candidate=candidate,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )

    assert targets == [target]
    assert images[target]["active"]["imageId"] != images[target]["candidate"]["imageId"]
    assert set(images) == set(stage_transition._runtime_service_inventory(active))
    assert all(row["configChanged"] is False and row["added"] is False for row in images.values())


def test_issued_cloud_image_update_preserves_the_exact_fourteen_service_inventory(
    tmp_path: Path,
) -> None:
    """Allow the issued Cloud route only when its one-image change keeps all 14 services."""

    active, candidate, active_lock, candidate_lock = _full_cloud_image_update(tmp_path)
    targets, images = stage_transition._validate_phase_topology(
        phase="issued-connector-activation",
        image_update_target="cloud-management-phase2",
        active=active,
        candidate=candidate,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )
    assert targets == ["cloud-management-phase2"]
    assert set(images) == stage_transition.RUNTIME_SERVICE_SET
    assert all(row["configChanged"] is False and row["added"] is False for row in images.values())

    active, candidate, active_lock, candidate_lock = _full_cloud_image_update(tmp_path)
    del active["services"]["computer-runtime"]
    del candidate["services"]["computer-runtime"]
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="exact 14-service topology or supported Graph/ADO extension",
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            image_update_target="cloud-management-phase2",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def test_issued_github_image_update_preserves_exact_fourteen_service_inventory(
    tmp_path: Path,
) -> None:
    """Allow only the locked GitHub provider image on the existing issued 14-service topology."""

    active, candidate, active_lock, candidate_lock = _full_github_image_update(tmp_path)
    targets, images = stage_transition._validate_phase_topology(
        phase="issued-connector-activation",
        image_update_target="github-issues",
        active=active,
        candidate=candidate,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )
    assert targets == ["github-issues"]
    assert (
        images["github-issues"]["active"]["imageId"]
        != images["github-issues"]["candidate"]["imageId"]
    )
    assert set(images) == stage_transition.RUNTIME_SERVICE_SET
    assert all(row["configChanged"] is False and row["added"] is False for row in images.values())

    with pytest.raises(
        stage_transition.StageTransitionError,
        match="GitHub Issues image updates require issued-connector-activation",
    ):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            image_update_target="github-issues",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _full_github_image_update(tmp_path)
    del active["services"]["computer-runtime"]
    del candidate["services"]["computer-runtime"]
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="exact 14-service topology or supported Graph/ADO extension",
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            image_update_target="github-issues",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def test_memory_update_changes_only_one_existing_service_no_swap_limit(tmp_path: Path) -> None:
    """Permit one strictly higher memory limit while retaining all 14 service images."""

    active, candidate, lock = _active_memory_update(tmp_path)
    targets, services = stage_transition._validate_phase_topology(
        phase="issued-connector-activation",
        memory_update_target="teams-ingress",
        active=active,
        candidate=candidate,
        active_lock=lock,
        candidate_lock=copy.deepcopy(lock),
    )

    assert targets == ["teams-ingress"]
    assert set(services) == stage_transition.RUNTIME_SERVICE_SET
    assert services["teams-ingress"]["configChanged"] is True
    assert all(
        row["added"] is False
        and row["configChanged"] is (name == "teams-ingress")
        and row["active"] == row["candidate"]
        for name, row in services.items()
    )


def test_memory_update_accepts_matching_optional_deploy_memory_limit(tmp_path: Path) -> None:
    """Keep a present deploy memory limit equal to the Compose service limit."""

    active, candidate, lock = _active_memory_update(tmp_path)
    active_service = active["services"]["teams-ingress"]
    candidate_service = candidate["services"]["teams-ingress"]
    active_service["deploy"] = {"resources": {"limits": {"memory": "128m", "cpus": "0.05"}}}
    candidate_service["deploy"] = {"resources": {"limits": {"memory": "256m", "cpus": "0.05"}}}

    targets, _services = stage_transition._validate_phase_topology(
        phase="issued-connector-activation",
        memory_update_target="teams-ingress",
        active=active,
        candidate=candidate,
        active_lock=lock,
        candidate_lock=lock,
    )
    assert targets == ["teams-ingress"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("decrease", "only increase"),
        ("swap", "no-swap"),
        ("cpu", "only the selected service memory limit fields"),
        ("security", "only the selected service memory limit fields"),
        ("lock", "exact image lock"),
        ("deploy_mismatch", "deploy.resources.limits.memory must match"),
        ("bootstrap_phase", "issued-connector-activation"),
    ],
)
def test_memory_update_rejects_decreases_and_unrelated_drift(
    tmp_path: Path, mutation: str, message: str
) -> None:
    """Reject decreases, swap, image, CPU, security, or phase changes before apply."""

    active, candidate, lock = _active_memory_update(tmp_path)
    target = candidate["services"]["teams-ingress"]
    phase = "issued-connector-activation"
    candidate_lock = copy.deepcopy(lock)
    if mutation == "decrease":
        target["mem_limit"] = target["memswap_limit"] = "64m"
    elif mutation == "swap":
        target["memswap_limit"] = "512m"
    elif mutation == "cpu":
        target["cpus"] = "0.10"
    elif mutation == "security":
        target["security_opt"] = ["no-new-privileges:true"]
    elif mutation == "lock":
        candidate_lock["images"][0]["imageId"] = _image_id(999)
    elif mutation == "deploy_mismatch":
        target["deploy"] = {"resources": {"limits": {"memory": "512m"}}}
    else:
        phase = "provider-bootstrap"

    with pytest.raises(stage_transition.StageTransitionError, match=message):
        stage_transition._validate_phase_topology(
            phase=phase,
            memory_update_target="teams-ingress",
            active=active,
            candidate=candidate,
            active_lock=lock,
            candidate_lock=candidate_lock,
        )


def test_memory_update_rejects_unbounded_inventory_and_non_target_selection(tmp_path: Path) -> None:
    """Require exactly 14 active services and a target from the runtime inventory."""

    active, candidate, lock = _active_memory_update(tmp_path)
    with pytest.raises(stage_transition.StageTransitionError, match="one existing runtime service"):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            memory_update_target="operator-bootstrap",
            active=active,
            candidate=candidate,
            active_lock=lock,
            candidate_lock=lock,
        )

    active, candidate, lock = _active_memory_update(tmp_path)
    del active["services"]["computer-runtime"]
    del candidate["services"]["computer-runtime"]
    with pytest.raises(stage_transition.StageTransitionError, match="exact active 14-service"):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            memory_update_target="teams-ingress",
            active=active,
            candidate=candidate,
            active_lock=lock,
            candidate_lock=lock,
        )


def test_memory_update_actual_capacity_checks_all_fourteen_live_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use actual Docker limits, not only Compose totals, for the host capacity gate."""

    compose, _candidate, _lock = _active_memory_update(tmp_path)
    live = stage_transition._runtime_service_inventory(compose)
    services: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    for index, name in enumerate(stage_transition.RUNTIME_SERVICE_ORDER, start=1):
        service = live[name]
        memory_bytes = preflight._memory_limit_bytes(service["mem_limit"])
        cpu_limit = preflight._cpu_limit_decimal(service["cpus"])
        assert memory_bytes is not None and cpu_limit is not None
        total_bytes += memory_bytes
        container_id = f"{index:064x}"
        image_id = _image_id(index)
        services[name] = {
            "containerId": container_id,
            "imageRef": service["image"],
            "imageId": image_id,
        }
        rows.append(
            {
                "Id": container_id,
                "Image": image_id,
                "Config": {
                    "Image": service["image"],
                    "Labels": {
                        "com.docker.compose.project": compose["name"],
                        "com.docker.compose.service": name,
                    },
                },
                "HostConfig": {
                    "Memory": memory_bytes,
                    "MemorySwap": memory_bytes,
                    "NanoCpus": int(cpu_limit * 1_000_000_000),
                },
            }
        )
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda _argv, **_kwargs: json.dumps(rows),
    )

    capacity = stage_transition._memory_update_actual_capacity(
        compose=compose,
        services=services,
        max_memory_mib=3584,
        max_cpus=1.95,
        docker_prefix=["docker"],
    )
    assert capacity["serviceCount"] == 14
    assert capacity["totalMemoryBytes"] == total_bytes
    assert capacity["maxMemoryBytes"] == 3584 * preflight.MIB
    assert capacity["totalNanoCpus"] <= 1_950_000_000

    rows[0]["HostConfig"]["Memory"] += preflight.MIB
    with pytest.raises(stage_transition.StageTransitionError, match="actual memory limits differ"):
        stage_transition._memory_update_actual_capacity(
            compose=compose,
            services=services,
            max_memory_mib=3584,
            max_cpus=1.95,
            docker_prefix=["docker"],
        )


def test_memory_update_process_identity_is_pinned_before_and_after() -> None:
    """Require the same healthy process, start time, and restart count around update."""

    active_service = {
        "containerId": "a" * 64,
        "imageRef": "example.invalid/teams-ingress:fixed",
        "imageId": _image_id(7),
    }
    planned_state = {
        "containerComposeConfigHash": "b" * 64,
        "containerConfigSha256": "c" * 64,
        "hostConfigSha256WithoutMemory": "d" * 64,
        "processId": 1879250,
        "processStartedAt": "2026-10-27T18:00:00.000000000Z",
        "restartCount": 4,
    }
    readback = {
        "containerId": active_service["containerId"],
        "imageRef": active_service["imageRef"],
        "imageId": active_service["imageId"],
        "composeConfigHash": planned_state["containerComposeConfigHash"],
        "containerConfigSha256": planned_state["containerConfigSha256"],
        "hostConfigSha256WithoutMemory": planned_state["hostConfigSha256WithoutMemory"],
        "processId": planned_state["processId"],
        "processStartedAt": planned_state["processStartedAt"],
        "restartCount": planned_state["restartCount"],
        "memoryBytes": 128 * preflight.MIB,
        "memorySwapBytes": 128 * preflight.MIB,
        "health": "healthy",
    }

    stage_transition._verify_memory_update_snapshot(
        readback,
        planned_state,
        active_service,
        expected_memory_bytes=128 * preflight.MIB,
    )

    for field, changed in (
        ("processId", planned_state["processId"] + 1),
        ("processStartedAt", "2026-10-27T18:00:01.000000000Z"),
        ("restartCount", planned_state["restartCount"] + 1),
    ):
        mutated_readback = dict(readback)
        mutated_readback[field] = changed
        with pytest.raises(stage_transition.StageTransitionError, match="read-back differs"):
            stage_transition._verify_memory_update_snapshot(
                mutated_readback,
                planned_state,
                active_service,
                expected_memory_bytes=128 * preflight.MIB,
            )


def _install_memory_update_apply_mocks(
    monkeypatch: pytest.MonkeyPatch, *, after_memory_bytes: int
) -> tuple[dict[str, Any], dict[str, Any], list[list[str]]]:
    """Prepare a journaled hot-update fixture with read-only Docker responses."""

    container_id = "a" * 64
    image_id = _image_id(7)
    actual_hash = "b" * 64
    config_hash = "c" * 64
    host_hash = "d" * 64
    state = {
        "activeMemoryBytes": 128 * preflight.MIB,
        "candidateMemoryBytes": 256 * preflight.MIB,
        "activeRuntimeMemoryBytes": 3392 * preflight.MIB,
        "activeRuntimeNanoCpus": 1_900_000_000,
        "activeComposeConfigHash": actual_hash,
        "candidateComposeConfigHash": "e" * 64,
        "containerComposeConfigHash": actual_hash,
        "configHashRelation": "MATCHES_ACTIVE",
        "configHashProofOperationId": None,
        "configHashProofPlanSha256": None,
        "containerConfigSha256": config_hash,
        "hostConfigSha256WithoutMemory": host_hash,
        "processId": 1879250,
        "processStartedAt": "2026-10-27T18:00:00.000000000Z",
        "restartCount": 4,
    }
    active_service = {
        "containerId": container_id,
        "imageRef": "example.invalid/teams-ingress:fixed",
        "imageId": image_id,
        "health": "healthy",
        "networkNames": ["teams-proxy-private"],
        "mounts": [],
    }
    plan = {
        "project": "cyrene-teams-cloud",
        "memoryUpdateTarget": "teams-ingress",
        "memoryUpdateState": state,
        "activeServices": {"teams-ingress": active_service},
        "candidateServices": {"teams-ingress": {"active": {}, "candidate": {}}},
        "runtimeConfigIdentity": {
            "active": {"teams-ingress": []},
            "candidate": {"teams-ingress": []},
        },
        "candidatePreflight": {
            "maxMemoryMiB": 3584,
            "maxCpus": 1.95,
            "minimumMemAvailableMiB": 1024,
        },
        "edgeDelivery": {"containerId": "f" * 64, "revision": 12, "deadCount": 0},
        "navigatorDrain": {
            "containerId": "9" * 64,
            "instanceId": "navigator-instance",
            "generation": 3,
        },
    }
    journal: dict[str, Any] = {
        "state": "RUNNING",
        "completedTargets": [],
        "targetProgress": {"teams-ingress": {"phase": "PREPARED", "applyIntent": False}},
        "memoryUpdate": {
            "required": True,
            "target": "teams-ingress",
            "plannedState": state,
            "attemptIntent": False,
            "beforeReadback": None,
            "afterReadback": None,
            "beforeRuntimeLimits": None,
            "afterRuntimeLimits": None,
            "beforeExchangeReadiness": None,
            "afterExchangeReadiness": None,
            "outcome": "NOT_STARTED",
        },
        "exchangeReadiness": {"required": True, "verified": False, "receipt": None},
        "edge": {"initialDeadCount": 0, "latestDeadCount": 0},
        "selection": {"promoted": False},
    }
    snapshots = [
        {
            "containerId": container_id,
            "imageRef": active_service["imageRef"],
            "imageId": image_id,
            "memoryBytes": 128 * preflight.MIB,
            "memorySwapBytes": 128 * preflight.MIB,
            "processId": 1879250,
            "processStartedAt": "2026-10-27T18:00:00.000000000Z",
            "restartCount": 4,
            "health": "healthy",
            "composeConfigHash": actual_hash,
            "containerConfigSha256": config_hash,
            "hostConfigSha256WithoutMemory": host_hash,
        },
        {
            "containerId": container_id,
            "imageRef": active_service["imageRef"],
            "imageId": image_id,
            "memoryBytes": after_memory_bytes,
            "memorySwapBytes": after_memory_bytes,
            "processId": 1879250,
            "processStartedAt": "2026-10-27T18:00:00.000000000Z",
            "restartCount": 4,
            "health": "healthy",
            "composeConfigHash": actual_hash,
            "containerConfigSha256": config_hash,
            "hostConfigSha256WithoutMemory": host_hash,
        },
    ]
    commands: list[list[str]] = []
    monkeypatch.setattr(
        stage_transition, "_memory_update_container_snapshot", lambda **_kwargs: snapshots.pop(0)
    )
    monkeypatch.setattr(
        stage_transition, "_verify_memory_update_config_hash_relation", lambda **_kwargs: None
    )
    capacities = iter(
        [
            {
                "serviceCount": 14,
                "totalMemoryBytes": 3392 * preflight.MIB,
                "totalNanoCpus": 1_900_000_000,
            },
            {
                "serviceCount": 14,
                "totalMemoryBytes": 3520 * preflight.MIB,
                "totalNanoCpus": 1_900_000_000,
            },
        ]
    )
    monkeypatch.setattr(
        stage_transition, "_memory_update_actual_capacity", lambda **_kwargs: next(capacities)
    )
    monkeypatch.setattr(
        stage_transition,
        "_record_exchange_readiness",
        lambda **_kwargs: journal["exchangeReadiness"].update(
            verified=True, receipt={"status": "LIVE_VERIFIED", "bindingCount": 14}
        ),
    )
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args, **_kwargs: {"paused": False, "revision": 12, "dead_count": 0},
    )
    monkeypatch.setattr(
        runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            "mode": "accepting",
            "instanceId": "navigator-instance",
            "generation": 3,
        },
    )
    monkeypatch.setattr(
        runtime_update,
        "_run_command",
        lambda argv, **_kwargs: commands.append(argv) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        stage_transition, "_live_service_inventory", lambda **_kwargs: plan["activeServices"]
    )
    monkeypatch.setattr(runtime_update, "_verify_live_network_membership", lambda *_args: None)
    monkeypatch.setattr(
        stage_transition,
        "_validate_post_update_capacity",
        lambda **_kwargs: {"memAvailableMiB": 2048, "totalRssMiB": 500},
    )
    monkeypatch.setattr(
        stage_transition,
        "_promote_selection",
        lambda **_kwargs: {"operationId": "memory-operation"},
    )
    monkeypatch.setattr(
        stage_transition, "_selection_summary", lambda _selection: {"promoted": True}
    )
    monkeypatch.setattr(
        stage_transition, "_journal_summary", lambda _journal: {"state": _journal["state"]}
    )

    def write_journal(
        _path: Path, current: dict[str, Any], phase: str, *, state: str | None = None
    ) -> None:
        current["phase"] = phase
        if state is not None:
            current["state"] = state

    monkeypatch.setattr(stage_transition, "_write_journal", write_journal)
    for name in ("_pause_edge", "_drain_navigator"):
        monkeypatch.setattr(
            runtime_update,
            name,
            lambda _name=name, **_kwargs: pytest.fail(f"unexpected {_name}"),
        )
    monkeypatch.setattr(
        stage_transition,
        "_atomic_sqlite_snapshot",
        lambda **_kwargs: pytest.fail("unexpected SQLite snapshot"),
    )
    monkeypatch.setattr(
        stage_transition,
        "_compose_target_update",
        lambda **_kwargs: pytest.fail("unexpected Compose recreation"),
    )
    return plan, journal, commands


def test_memory_update_uses_one_docker_update_without_pausing_or_recreating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply one in-place memory/swap increase and preserve ingress and Navigator."""

    plan, journal, commands = _install_memory_update_apply_mocks(
        monkeypatch, after_memory_bytes=256 * preflight.MIB
    )
    result = stage_transition._apply_memory_update(
        plan=plan,
        active_compose={"name": "cyrene-teams-cloud", "services": {}},
        candidate_compose={"name": "cyrene-teams-cloud", "services": {}},
        candidate_lock={},
        journal=journal,
        journal_path=tmp_path / "journal.json",
        docker_prefix=["docker"],
    )

    assert commands == [
        [
            "docker",
            "update",
            "--memory",
            str(256 * preflight.MIB),
            "--memory-swap",
            str(256 * preflight.MIB),
            plan["activeServices"]["teams-ingress"]["containerId"],
        ]
    ]
    assert result["state"] == "APPLIED"
    assert journal["targetProgress"]["teams-ingress"]["phase"] == "APPLIED"
    assert journal["memoryUpdate"]["afterRuntimeLimits"]["totalMemoryBytes"] == 3520 * preflight.MIB


def test_memory_update_partial_readback_requires_recovery_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never lower or replay a limit when the single Docker update reads back partially."""

    plan, journal, commands = _install_memory_update_apply_mocks(
        monkeypatch, after_memory_bytes=192 * preflight.MIB
    )
    with pytest.raises(
        stage_transition.StageTransitionError, match="partial or its identity changed"
    ):
        stage_transition._apply_memory_update(
            plan=plan,
            active_compose={"name": "cyrene-teams-cloud", "services": {}},
            candidate_compose={"name": "cyrene-teams-cloud", "services": {}},
            candidate_lock={},
            journal=journal,
            journal_path=tmp_path / "journal.json",
            docker_prefix=["docker"],
        )

    assert len(commands) == 1
    assert journal["state"] == "RECOVERY_REQUIRED"
    assert journal["memoryUpdate"]["outcome"] == "RECOVERY_REQUIRED"
    assert journal["selection"]["promoted"] is False


def test_memory_update_timeout_stays_recovery_required_even_if_first_readback_is_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Treat a timed-out Docker API as still in flight even when one read sees old limits."""

    plan, journal, commands = _install_memory_update_apply_mocks(
        monkeypatch, after_memory_bytes=128 * preflight.MIB
    )

    def timed_out(argv: list[str], **_kwargs: Any) -> None:
        commands.append(argv)
        raise runtime_update.RuntimeUpdateOutcomeUnknown("simulated timeout")

    monkeypatch.setattr(runtime_update, "_run_command", timed_out)
    with pytest.raises(
        stage_transition.StageTransitionError, match="daemon outcome may still complete"
    ):
        stage_transition._apply_memory_update(
            plan=plan,
            active_compose={"name": "cyrene-teams-cloud", "services": {}},
            candidate_compose={"name": "cyrene-teams-cloud", "services": {}},
            candidate_lock={},
            journal=journal,
            journal_path=tmp_path / "journal.json",
            docker_prefix=["docker"],
        )

    assert len(commands) == 1
    assert journal["state"] == "RECOVERY_REQUIRED"
    assert journal["memoryUpdate"]["outcome"] == "RECOVERY_REQUIRED"
    assert journal["selection"]["promoted"] is False


def test_memory_update_stale_compose_hash_follows_existing_selector_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carry a stale config-hash through promotion, even after a later process restart."""

    memory_operation = "00000000-0000-4000-8000-000000000001"
    normal_operation = "00000000-0000-4000-8000-000000000002"
    current_selector = {
        "schemaVersion": stage_transition.SELECTION_SCHEMA,
        "operationId": normal_operation,
    }
    previous_selector = {
        "schemaVersion": stage_transition.SELECTION_SCHEMA,
        "operationId": memory_operation,
    }
    current_sha = stage_transition._sha256_bytes(
        stage_transition._canonical_json(current_selector) + b"\n"
    )
    previous_sha = stage_transition._sha256_bytes(
        stage_transition._canonical_json(previous_selector) + b"\n"
    )
    active_hash = "a" * 64
    container_hash = "b" * 64
    plan_sha_by_op = {memory_operation: "c" * 64, normal_operation: "d" * 64}
    plan_by_path = {
        str(tmp_path / f"{memory_operation}.plan.json"): {
            "operationId": memory_operation,
            "planSha256": plan_sha_by_op[memory_operation],
            "memoryUpdateTarget": "teams-ingress",
            "memoryUpdateState": {
                "candidateComposeConfigHash": active_hash,
                "containerComposeConfigHash": container_hash,
                "processId": 100,
                "processStartedAt": "2026-10-27T17:00:00.000000000Z",
                "restartCount": 3,
            },
            "candidateSelectionFile": "/private/runtime-selection.json",
            "activeServices": {
                "teams-ingress": {
                    "containerId": "e" * 64,
                    "imageId": _image_id(1),
                    "processId": 100,
                }
            },
            "activeSelection": {"document": None},
        },
        str(tmp_path / f"{normal_operation}.plan.json"): {
            "operationId": normal_operation,
            "planSha256": plan_sha_by_op[normal_operation],
            "candidateSelectionFile": "/private/runtime-selection.json",
            "activeSelection": {
                "document": previous_selector,
                "sha256": previous_sha,
            },
        },
    }
    for plan_path in plan_by_path:
        path = Path(plan_path)
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)
    journal_by_id = {
        memory_operation: {
            "state": "APPLIED",
            "planSha256": plan_sha_by_op[memory_operation],
            "planPath": str(tmp_path / f"{memory_operation}.plan.json"),
            "selection": {
                "path": "/private/runtime-selection.json",
                "promoted": True,
                "newSha256": previous_sha,
            },
            "memoryUpdate": {
                "required": True,
                "target": "teams-ingress",
                "outcome": "APPLIED_SAME_CONTAINER",
                "afterReadback": {
                    "containerId": "e" * 64,
                    "composeConfigHash": container_hash,
                    "processId": 100,
                    "processStartedAt": "2026-10-27T17:00:00.000000000Z",
                    "restartCount": 3,
                },
            },
        },
        normal_operation: {
            "state": "APPLIED",
            "planSha256": plan_sha_by_op[normal_operation],
            "planPath": str(tmp_path / f"{normal_operation}.plan.json"),
            "selection": {
                "path": "/private/runtime-selection.json",
                "promoted": True,
                "newSha256": current_sha,
            },
        },
    }
    monkeypatch.setattr(
        stage_transition,
        "_journal_path",
        lambda _directory, operation_id: tmp_path / f"{operation_id}.json",
    )
    monkeypatch.setattr(
        stage_transition,
        "_read_journal",
        lambda path: journal_by_id[Path(path).stem],
    )
    monkeypatch.setattr(
        stage_transition,
        "_read_json",
        lambda path, _label: plan_by_path[str(path)],
    )
    monkeypatch.setattr(stage_transition, "_validate_plan", lambda _plan: None)
    monkeypatch.setattr(preflight, "_private_artifact", lambda _path: True)

    proof = stage_transition._prior_memory_update_hash_proof(
        selection_record={"document": current_selector, "sha256": current_sha},
        selection_path="/private/runtime-selection.json",
        journal_dir=tmp_path,
        target="teams-ingress",
        active_compose_hash=active_hash,
        container_compose_hash=container_hash,
        active_service={
            "containerId": "e" * 64,
            "imageId": _image_id(1),
            "processId": 201,
            "processStartedAt": "2026-10-27T18:00:00.000000000Z",
            "restartCount": 4,
        },
    )
    assert proof == {"operationId": memory_operation, "planSha256": "c" * 64}

    assert (
        stage_transition._prior_memory_update_hash_proof(
            selection_record={"document": current_selector, "sha256": current_sha},
            selection_path="/private/runtime-selection.json",
            journal_dir=tmp_path,
            target="teams-ingress",
            active_compose_hash=active_hash,
            container_compose_hash=container_hash,
            active_service={
                "containerId": "f" * 64,
                "imageId": _image_id(1),
                "processId": 201,
                "processStartedAt": "2026-10-27T18:00:00.000000000Z",
                "restartCount": 4,
            },
        )
        is None
    )


def test_memory_update_compose_hash_uses_canonical_profile_and_output_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use Compose's selected-service hash and reject any ambiguous CLI output."""

    calls: list[list[str]] = []
    expected_hash = "a" * 64
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda argv, **_kwargs: calls.append(argv) or f"teams-ingress {expected_hash}\n",
    )
    actual_hash = stage_transition._compose_service_config_hash(
        service_name="teams-ingress",
        compose_files=[tmp_path / "active.yaml"],
        env_files=[tmp_path / "runtime.env"],
        docker_prefix=["docker"],
    )
    assert actual_hash == expected_hash
    assert calls[0][-3:] == ["config", "--hash", "teams-ingress"]
    assert [
        calls[0][index + 1] for index, item in enumerate(calls[0][:-1]) if item == "--profile"
    ] == list(preflight.ACTIVE_COMPOSE_PROFILES)

    monkeypatch.setattr(runtime_update, "_checked_stdout", lambda *_args, **_kwargs: "ambiguous")
    with pytest.raises(stage_transition.StageTransitionError, match="config hash is malformed"):
        stage_transition._compose_service_config_hash(
            service_name="teams-ingress",
            compose_files=[tmp_path / "active.yaml"],
            env_files=[tmp_path / "runtime.env"],
            docker_prefix=["docker"],
        )


def test_listener_repair_changes_only_the_two_wildcard_command_arrays(
    tmp_path: Path,
) -> None:
    """Keep all 14 service images fixed while replacing only the two listener arguments.

    中文：目标只能是 HF 与 Cloud，且两组 argv 除 listen 地址外必须逐项一致。
    """

    active, candidate, active_lock, candidate_lock = _listener_repair_pair(tmp_path)
    targets, service_images = stage_transition._validate_phase_topology(
        phase="provider-bootstrap",
        listener_repair=True,
        active=active,
        candidate=candidate,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )

    assert targets == ["huggingface", "cloud-management-phase2"]
    assert set(service_images) == stage_transition.RUNTIME_SERVICE_SET
    assert all(row["active"] == row["candidate"] for row in service_images.values())
    assert all(
        row["added"] is False and row["configChanged"] is False for row in service_images.values()
    )
    for service_name in stage_transition.PROVIDER_LISTENER_REPAIR_TARGETS:
        old_command = list(stage_transition.PROVIDER_DEFAULT_COMMANDS[service_name])
        new_command = list(stage_transition.PROVIDER_WILDCARD_COMMANDS[service_name])
        listen_index = old_command.index("--listen")
        old_command[listen_index + 1] = "0.0.0.0:7443"
        assert new_command == old_command


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("hostname", "candidate huggingface command must change only its listener"),
        ("extra_arg", "candidate cloud-management-phase2 command must change only its listener"),
        ("environment", "may change only the HF and Cloud listener commands"),
        ("image_lock", "preserve the exact image lock"),
        ("missing_service", "exact 14-service topology"),
        ("extra_service", "exact 14-service topology"),
    ],
)
def test_listener_repair_rejects_anything_beyond_the_two_listener_commands(
    tmp_path: Path, mutation: str, message: str
) -> None:
    """Reject other argv, config, image-lock, or topology changes in repair mode."""

    active, candidate, active_lock, candidate_lock = _listener_repair_pair(tmp_path)
    if mutation == "hostname":
        candidate["services"]["huggingface"]["command"][
            candidate["services"]["huggingface"]["command"].index("--listen") + 1
        ] = "huggingface:7443"
    elif mutation == "extra_arg":
        candidate["services"]["cloud-management-phase2"]["command"].append("--debug")
    elif mutation == "environment":
        candidate["services"]["huggingface"]["environment"]["UNEXPECTED"] = "true"
    elif mutation == "image_lock":
        candidate_lock["images"][0]["imageId"] = _image_id(90)
    elif mutation == "missing_service":
        del candidate["services"]["account-center"]
    elif mutation == "extra_service":
        active["services"]["unexpected-runtime"] = _service("unexpected:fixed")
        candidate["services"]["unexpected-runtime"] = _service("unexpected:fixed")

    with pytest.raises(stage_transition.StageTransitionError, match=message):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            listener_repair=True,
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def test_listener_repair_is_not_available_during_issued_activation(tmp_path: Path) -> None:
    """Keep this listener repair closed to the bootstrap runner-off phase."""

    active, candidate, active_lock, candidate_lock = _listener_repair_pair(tmp_path)
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="limited to provider-bootstrap",
    ):
        stage_transition._validate_phase_topology(
            phase="issued-connector-activation",
            listener_repair=True,
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def _listener_repair_state(active_services: dict[str, Any]) -> dict[str, Any]:
    """Build one sealed command and pre-repair issued-state record for local tests."""

    return {
        "services": {
            service_name: {
                "containerId": active_services[service_name]["containerId"],
                "imageId": active_services[service_name]["imageId"],
                "activeCommand": list(stage_transition.PROVIDER_DEFAULT_COMMANDS[service_name]),
                "candidateCommand": list(stage_transition.PROVIDER_WILDCARD_COMMANDS[service_name]),
                "environmentSha256": stage_transition._docker_environment_sha256(
                    ["PATH=/usr/bin", f"PROVIDER_ENV={service_name}"]
                ),
            }
            for service_name in stage_transition.PROVIDER_LISTENER_REPAIR_TARGETS
        },
        "issuedState": {
            "exchangeContainerId": active_services["exchange"]["containerId"],
            "exchangeImageId": active_services["exchange"]["imageId"],
            "issuedStateSha256": "a" * 64,
            "catalogStatus": "NOT_LIVE_CATALOG",
            "profileCount": 5,
            "connectionCount": 14,
            "bindingCount": 14,
            "verifiedProviderIds": [
                "azure",
                "computer",
                "gcp",
                "github",
                "huggingface",
            ],
        },
    }


def test_listener_repair_environment_fingerprint_is_order_independent_and_closed() -> None:
    """Hash Docker Config.Env without accepting ambiguous names or retaining values."""

    environment = ["API_MODE=read-only", "PATH=/usr/bin"]
    fingerprint = stage_transition._docker_environment_sha256(environment)
    assert fingerprint == stage_transition._docker_environment_sha256(list(reversed(environment)))
    assert len(fingerprint) == 64
    for malformed in (["MISSING_SEPARATOR"], ["TOKEN=one", "TOKEN=two"]):
        with pytest.raises(
            stage_transition.StageTransitionError,
            match="environment readback is (malformed|ambiguous)",
        ):
            stage_transition._docker_environment_sha256(malformed)


def test_listener_repair_state_reads_exact_active_commands_and_validates_target_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind both old and new command arrays to their exact service container IDs."""

    active, candidate, _active_lock, _candidate_lock = _listener_repair_pair(tmp_path)
    active_services = {
        service_name: {"containerId": f"{index:064x}", "imageId": _image_id(index)}
        for index, service_name in enumerate(
            stage_transition.PROVIDER_LISTENER_REPAIR_TARGETS, start=30
        )
    }
    active_services["exchange"] = {"containerId": "e" * 64, "imageId": _image_id(40)}

    def checked(argv: list[str], **_kwargs: Any) -> str:
        container_id = argv[-1]
        service_name = next(
            name for name, row in active_services.items() if row.get("containerId") == container_id
        )
        if "{{json .Config.Cmd}}" in argv:
            return json.dumps(list(stage_transition.PROVIDER_DEFAULT_COMMANDS[service_name]))
        return json.dumps(["PATH=/usr/bin", f"PROVIDER_ENV={service_name}"])

    monkeypatch.setattr(runtime_update, "_checked_stdout", checked)
    state = stage_transition._listener_repair_state_record(
        active_compose=active,
        candidate_compose=candidate,
        active_services=active_services,
        docker_prefix=["docker"],
    )
    state["issuedState"] = _listener_repair_state(active_services)["issuedState"]
    assert stage_transition._validate_listener_repair_state_record(state, active_services) == state

    bad = copy.deepcopy(state)
    bad["services"]["huggingface"]["candidateCommand"].append("--debug")
    with pytest.raises(
        stage_transition.StageTransitionError, match="command identity is malformed"
    ):
        stage_transition._validate_listener_repair_state_record(bad, active_services)

    active_command_by_id = {
        row["containerId"]: list(stage_transition.PROVIDER_DEFAULT_COMMANDS[name])
        for name, row in state["services"].items()
    }
    monkeypatch.setattr(
        stage_transition,
        "_service_command_readback",
        lambda **kwargs: active_command_by_id[kwargs["container_id"]],
    )
    monkeypatch.setattr(
        stage_transition,
        "_read_live_exchange_issued_state",
        lambda **_kwargs: {
            "exchangeContainerId": "e" * 64,
            "exchangeImageId": _image_id(40),
            "issuedStateSha256": "a" * 64,
            "catalogStatus": "NOT_LIVE_CATALOG",
            "profileCount": 5,
            "connectionCount": 14,
            "bindingCount": 14,
            "verifiedProviderIds": ["azure", "computer", "gcp", "github", "huggingface"],
        },
    )
    environment_sha_by_id = {
        row["containerId"]: row["environmentSha256"] for row in state["services"].values()
    }
    monkeypatch.setattr(
        stage_transition,
        "_service_environment_sha256",
        lambda **kwargs: environment_sha_by_id[kwargs["container_id"]],
    )
    plan = {"listenerRepairState": state, "activeServices": active_services}
    stage_transition._verify_listener_repair_active_state(
        plan=plan,
        active_compose=active,
        docker_prefix=["docker"],
    )
    active_command_by_id[active_services["huggingface"]["containerId"]] = ["wrong"]
    with pytest.raises(stage_transition.StageTransitionError, match="changed after planning"):
        stage_transition._verify_listener_repair_active_state(
            plan=plan,
            active_compose=active,
            docker_prefix=["docker"],
        )
    active_command_by_id[active_services["huggingface"]["containerId"]] = list(
        stage_transition.PROVIDER_DEFAULT_COMMANDS["huggingface"]
    )
    environment_sha_by_id[active_services["huggingface"]["containerId"]] = "b" * 64
    with pytest.raises(stage_transition.StageTransitionError, match="environment changed"):
        stage_transition._verify_listener_repair_active_state(
            plan=plan,
            active_compose=active,
            docker_prefix=["docker"],
        )
    environment_sha_by_id[active_services["huggingface"]["containerId"]] = state["services"][
        "huggingface"
    ]["environmentSha256"]

    journal = {
        "targetProgress": {
            service_name: {"candidateContainerId": f"{index:064x}"}
            for index, service_name in enumerate(
                stage_transition.PROVIDER_LISTENER_REPAIR_TARGETS, start=50
            )
        }
    }
    command_by_id = {
        row["candidateContainerId"]: list(stage_transition.PROVIDER_WILDCARD_COMMANDS[name])
        for name, row in journal["targetProgress"].items()
    }
    environment_sha_by_id.update(
        {
            row["candidateContainerId"]: state["services"][name]["environmentSha256"]
            for name, row in journal["targetProgress"].items()
        }
    )
    monkeypatch.setattr(
        stage_transition,
        "_service_command_readback",
        lambda **kwargs: command_by_id[kwargs["container_id"]],
    )
    stage_transition._verify_listener_repair_target_commands(
        plan=plan,
        journal=journal,
        docker_prefix=["docker"],
    )
    active_command_by_id[active_services["huggingface"]["containerId"]] = list(
        stage_transition.PROVIDER_DEFAULT_COMMANDS["huggingface"]
    )
    command_by_id[next(iter(command_by_id))] = ["wrong"]
    with pytest.raises(
        stage_transition.StageTransitionError, match="exact wildcard listener command"
    ):
        stage_transition._verify_listener_repair_target_commands(
            plan=plan,
            journal=journal,
            docker_prefix=["docker"],
        )

    first_candidate_id = next(iter(command_by_id))
    command_by_id[first_candidate_id] = list(
        stage_transition.PROVIDER_WILDCARD_COMMANDS["huggingface"]
    )
    environment_sha_by_id[first_candidate_id] = "b" * 64
    with pytest.raises(stage_transition.StageTransitionError, match="listener environment"):
        stage_transition._verify_listener_repair_target_commands(
            plan=plan,
            journal=journal,
            docker_prefix=["docker"],
        )


def test_cloud_image_update_runner_state_is_bound_to_exact_exchange_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require bootstrap Compose and the live Exchange container to both disable the runner.

    中文：只核对精确 Exchange 容器、DB 路径和零待处理计数，不保留其他 env。
    """

    exchange_container = {
        "containerId": "a" * 64,
        "imageId": _image_id(54),
    }
    compose = {
        "services": {
            "exchange": {
                "environment": {
                    stage_transition.EXCHANGE_CLOUD_RUNNER_ENV: "false",
                }
            }
        }
    }
    docker_requests: list[list[str]] = []

    def checked_stdout(argv: list[str], **_kwargs: Any) -> str:
        docker_requests.append(argv)
        if "{{json .Config.Cmd}}" in argv:
            return json.dumps(
                ["serve", "--integration-database", stage_transition.EXCHANGE_INTEGRATION_DATABASE]
            )
        if "{{json .Config.Env}}" in argv:
            return json.dumps(
                [
                    "SENSITIVE_ENV_VALUE=must-not-enter-the-plan",
                    f"{stage_transition.EXCHANGE_CLOUD_RUNNER_ENV}=false",
                ]
            )
        return json.dumps(
            {
                "cloudRefreshNonterminalCount": 0,
                "cloudRefreshActiveLeaseCount": 0,
                "authorizationMonitorActiveLeaseCount": 0,
                "emailNotificationNonterminalCount": 0,
            }
        )

    monkeypatch.setattr(runtime_update, "_checked_stdout", checked_stdout)
    state = stage_transition._cloud_runner_state_record(
        active_compose=compose,
        candidate_compose=copy.deepcopy(compose),
        exchange_service=exchange_container,
        docker_prefix=["docker"],
    )

    assert state == {
        "exchangeContainerId": "a" * 64,
        "exchangeImageId": _image_id(54),
        "integrationDatabasePath": stage_transition.EXCHANGE_INTEGRATION_DATABASE,
        "environmentName": stage_transition.EXCHANGE_CLOUD_RUNNER_ENV,
        "activeComposeValue": "false",
        "candidateComposeValue": "false",
        "liveContainerValue": "false",
        "idleCounts": {
            "cloudRefreshNonterminalCount": 0,
            "cloudRefreshActiveLeaseCount": 0,
            "authorizationMonitorActiveLeaseCount": 0,
            "emailNotificationNonterminalCount": 0,
        },
    }
    assert all("SENSITIVE_ENV_VALUE" not in str(value) for value in state.values())
    assert len(docker_requests) == 3
    assert docker_requests[0][2:4] == ["--format", "{{json .Config.Cmd}}"]
    assert docker_requests[1][2:4] == ["--format", "{{json .Config.Env}}"]
    idle_command = docker_requests[2]
    assert idle_command[1:4] == ["exec", "--user", "10001:10001"]
    idle_script = idle_command[idle_command.index("-c") + 1]
    assert "mode=ro" in idle_script and "PRAGMA query_only=ON" in idle_script
    assert "cloud_refresh_outbox" in idle_script
    assert "authorization_monitor_state" in idle_script
    assert "email_notification_outbox" in idle_script
    assert "document" not in idle_script
    assert (
        stage_transition._validate_cloud_runner_state_record(
            state,
            {"exchange": exchange_container},
        )
        == state
    )


@pytest.mark.parametrize(
    ("active_value", "candidate_value", "container_environment"),
    [
        ("true", "false", [f"{stage_transition.EXCHANGE_CLOUD_RUNNER_ENV}=false"]),
        ("false", "true", [f"{stage_transition.EXCHANGE_CLOUD_RUNNER_ENV}=false"]),
        ("false", "false", [f"{stage_transition.EXCHANGE_CLOUD_RUNNER_ENV}=true"]),
        ("false", "false", []),
        (
            "false",
            "false",
            [
                f"{stage_transition.EXCHANGE_CLOUD_RUNNER_ENV}=false",
                f"{stage_transition.EXCHANGE_CLOUD_RUNNER_ENV}=false",
            ],
        ),
    ],
)
def test_cloud_image_update_fails_closed_when_exchange_runner_is_not_exactly_disabled(
    monkeypatch: pytest.MonkeyPatch,
    active_value: str,
    candidate_value: str,
    container_environment: list[str],
) -> None:
    """Reject runner activity, missing flags, and duplicate runtime env entries.

    中文：env 不明确为单个 false 时，Cloud 镜像更新必须失败关闭。
    """

    def compose(value: str) -> dict[str, Any]:
        return {
            "services": {
                "exchange": {"environment": {stage_transition.EXCHANGE_CLOUD_RUNNER_ENV: value}}
            }
        }

    def checked_stdout(argv: list[str], **_kwargs: Any) -> str:
        if "{{json .Config.Cmd}}" in argv:
            return json.dumps(
                ["serve", "--integration-database", stage_transition.EXCHANGE_INTEGRATION_DATABASE]
            )
        if "{{json .Config.Env}}" in argv:
            return json.dumps(container_environment)
        return json.dumps(
            {
                "cloudRefreshNonterminalCount": 0,
                "cloudRefreshActiveLeaseCount": 0,
                "authorizationMonitorActiveLeaseCount": 0,
                "emailNotificationNonterminalCount": 0,
            }
        )

    monkeypatch.setattr(runtime_update, "_checked_stdout", checked_stdout)

    with pytest.raises(stage_transition.StageTransitionError):
        stage_transition._cloud_runner_state_record(
            active_compose=compose(active_value),
            candidate_compose=compose(candidate_value),
            exchange_service={"containerId": "b" * 64, "imageId": _image_id(55)},
            docker_prefix=["docker"],
        )


def test_cloud_runner_plan_record_rejects_changed_exchange_identity() -> None:
    """Bind runner evidence to the planned Exchange container and immutable image.

    中文：计划不能跨 Exchange 容器或镜像复用。
    """

    state = {
        "exchangeContainerId": "a" * 64,
        "exchangeImageId": _image_id(54),
        "integrationDatabasePath": stage_transition.EXCHANGE_INTEGRATION_DATABASE,
        "environmentName": stage_transition.EXCHANGE_CLOUD_RUNNER_ENV,
        "activeComposeValue": "false",
        "candidateComposeValue": "false",
        "liveContainerValue": "false",
        "idleCounts": {
            "cloudRefreshNonterminalCount": 0,
            "cloudRefreshActiveLeaseCount": 0,
            "authorizationMonitorActiveLeaseCount": 0,
            "emailNotificationNonterminalCount": 0,
        },
    }
    active_services = {"exchange": {"containerId": "a" * 64, "imageId": _image_id(54)}}
    assert stage_transition._validate_cloud_runner_state_record(state, active_services) == state

    with pytest.raises(stage_transition.StageTransitionError, match="runner-state evidence"):
        stage_transition._validate_cloud_runner_state_record(
            state,
            {"exchange": {"containerId": "c" * 64, "imageId": _image_id(54)}},
        )

    unsafe = {**state, "liveContainerValue": "true"}
    with pytest.raises(stage_transition.StageTransitionError, match="runner-state evidence"):
        stage_transition._validate_cloud_runner_state_record(unsafe, active_services)


def test_cloud_runner_idle_recheck_is_bound_to_the_frozen_plan_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan/apply checkpoints must observe the exact sealed Exchange idle record.

    中文：云作业、授权监视租约或邮件队列变化都必须阻断切换。
    """

    state = {
        "exchangeContainerId": "a" * 64,
        "exchangeImageId": _image_id(54),
        "integrationDatabasePath": stage_transition.EXCHANGE_INTEGRATION_DATABASE,
        "environmentName": stage_transition.EXCHANGE_CLOUD_RUNNER_ENV,
        "activeComposeValue": "false",
        "candidateComposeValue": "false",
        "liveContainerValue": "false",
        "idleCounts": {
            "cloudRefreshNonterminalCount": 0,
            "cloudRefreshActiveLeaseCount": 0,
            "authorizationMonitorActiveLeaseCount": 0,
            "emailNotificationNonterminalCount": 0,
        },
    }
    plan = {
        "cloudRunnerState": state,
        "activeServices": {"exchange": {"containerId": "a" * 64, "imageId": _image_id(54)}},
    }
    active_compose = {"services": {"exchange": {}}}
    candidate_compose = {"services": {"exchange": {}}}
    observed = {"value": state}
    monkeypatch.setattr(
        stage_transition,
        "_cloud_runner_state_record",
        lambda **_kwargs: observed["value"],
    )

    assert (
        stage_transition._verify_cloud_runner_idle(
            plan=plan,
            active_compose=active_compose,
            candidate_compose=candidate_compose,
            docker_prefix=["docker"],
        )
        == state
    )

    observed["value"] = {
        **state,
        "idleCounts": {**state["idleCounts"], "cloudRefreshNonterminalCount": 1},
    }
    with pytest.raises(
        stage_transition.StageTransitionError, match="changed after Cloud update planning"
    ):
        stage_transition._verify_cloud_runner_idle(
            plan=plan,
            active_compose=active_compose,
            candidate_compose=candidate_compose,
            docker_prefix=["docker"],
        )


def test_cloud_image_update_requires_all_exchange_job_queues_and_leases_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block replacement while any fixed Exchange background queue or lease is active.

    中文：Cloud refresh、授权监视器或邮件队列任一非空都不能重启 Cloud provider。
    """

    compose = {
        "services": {
            "exchange": {
                "environment": {
                    stage_transition.EXCHANGE_CLOUD_RUNNER_ENV: "false",
                }
            }
        }
    }

    def checked_stdout(argv: list[str], **_kwargs: Any) -> str:
        if "{{json .Config.Cmd}}" in argv:
            return json.dumps(
                ["serve", "--integration-database", stage_transition.EXCHANGE_INTEGRATION_DATABASE]
            )
        if "{{json .Config.Env}}" in argv:
            return json.dumps([f"{stage_transition.EXCHANGE_CLOUD_RUNNER_ENV}=false"])
        return json.dumps(
            {
                "cloudRefreshNonterminalCount": 0,
                "cloudRefreshActiveLeaseCount": 1,
                "authorizationMonitorActiveLeaseCount": 0,
                "emailNotificationNonterminalCount": 0,
            }
        )

    monkeypatch.setattr(runtime_update, "_checked_stdout", checked_stdout)
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="queued/running cloud, authorization-monitor, or email work",
    ):
        stage_transition._cloud_runner_state_record(
            active_compose=compose,
            candidate_compose=copy.deepcopy(compose),
            exchange_service={"containerId": "d" * 64, "imageId": _image_id(56)},
            docker_prefix=["docker"],
        )


def test_cloud_image_update_requires_exact_container_database_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not inspect a guessed Exchange DB when the live command selects another path.

    中文：活动命令未固定正式数据库路径时，读取检查失败关闭。
    """

    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: json.dumps(
            ["serve", "--integration-database", "/tmp/unexpected.sqlite3"]
        ),
    )
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="not pinned to the canonical integration database path",
    ):
        stage_transition._exchange_integration_database_container_path(
            container_id="e" * 64,
            docker_prefix=["docker"],
        )


def test_cloud_image_update_rechecks_active_exchange_runner_before_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject active Exchange runner drift before ingress pause or Navigator drain.

    中文：活动 runner 状态不再与冻结回执一致时，不开始 Cloud 替换。
    """

    exchange_id = "a" * 64
    exchange_image_id = _image_id(54)
    navigator_id = "b" * 64
    edge_id = "c" * 64
    instance_id = "11111111-1111-4111-8111-111111111111"
    compose = {
        "name": "cyrene-teams-cloud",
        "services": {
            "exchange": {
                "environment": {
                    stage_transition.EXCHANGE_CLOUD_RUNNER_ENV: "false",
                }
            }
        },
    }
    active_services = {
        "exchange": {"containerId": exchange_id, "imageId": exchange_image_id},
        "navigator": {"containerId": navigator_id, "imageId": _image_id(2)},
        "teams-ingress": {"containerId": edge_id, "imageId": _image_id(3)},
    }
    cloud_state = {
        "exchangeContainerId": exchange_id,
        "exchangeImageId": exchange_image_id,
        "integrationDatabasePath": stage_transition.EXCHANGE_INTEGRATION_DATABASE,
        "environmentName": stage_transition.EXCHANGE_CLOUD_RUNNER_ENV,
        "activeComposeValue": "false",
        "candidateComposeValue": "false",
        "liveContainerValue": "false",
        "idleCounts": {
            "cloudRefreshNonterminalCount": 0,
            "cloudRefreshActiveLeaseCount": 0,
            "authorizationMonitorActiveLeaseCount": 0,
            "emailNotificationNonterminalCount": 0,
        },
    }
    monkeypatch.setattr(stage_transition.preflight, "_compose_config", lambda *_args: compose)
    monkeypatch.setattr(runtime_update, "_load_lock", lambda _path: {})
    monkeypatch.setattr(
        stage_transition,
        "_live_service_inventory",
        lambda **_kwargs: active_services,
    )
    live_state = {"value": cloud_state}
    monkeypatch.setattr(
        stage_transition,
        "_cloud_runner_state_record",
        lambda **_kwargs: live_state["value"],
    )
    monkeypatch.setattr(
        runtime_update,
        "_current_edge_status",
        lambda *_args, **_kwargs: {"paused": False, "revision": 7, "dead_count": 0},
    )
    monkeypatch.setattr(
        runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            "mode": "accepting",
            "instanceId": instance_id,
            "generation": 4,
        },
    )
    plan = {
        "imageUpdateTarget": "cloud-management-phase2",
        "project": "cyrene-teams-cloud",
        "activeInputs": {
            "composeFiles": [],
            "envFiles": [],
            "imageLock": {"path": "/private/active-image-lock.json"},
        },
        "runtimeConfigIdentity": {"active": {}},
        "activeServices": active_services,
        "cloudRunnerState": cloud_state,
        "edgeDelivery": {"containerId": edge_id, "revision": 7, "deadCount": 0},
        "navigatorDrain": {
            "containerId": navigator_id,
            "instanceId": instance_id,
            "generation": 4,
        },
    }

    stage_transition._verify_active_baseline(plan, ["docker"])

    live_state["value"] = {**cloud_state, "liveContainerValue": "true"}
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="runner or durable cloud/email work is no longer idle",
    ):
        stage_transition._verify_active_baseline(plan, ["docker"])


def test_cloud_candidate_render_keeps_exchange_runner_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-render the frozen candidate before apply and keep its Exchange runner off.

    中文：候选 Compose 不能重新启用背景 runner。
    """

    candidate = {
        "name": "cyrene-teams-cloud",
        "services": {
            "exchange": {
                "environment": {
                    stage_transition.EXCHANGE_CLOUD_RUNNER_ENV: "false",
                }
            }
        },
    }
    monkeypatch.setattr(runtime_update, "_load_lock", lambda _path: {})
    monkeypatch.setattr(stage_transition.preflight, "_compose_config", lambda *_args: candidate)
    plan = {
        "imageUpdateTarget": "cloud-management-phase2",
        "project": "cyrene-teams-cloud",
        "candidateInputs": {
            "composeFiles": [],
            "envFiles": [],
            "imageLock": {"path": "/private/candidate-image-lock.json"},
        },
        "activeServices": {"exchange": {"containerId": "a" * 64, "imageId": _image_id(54)}},
        "cloudRunnerState": {
            "exchangeContainerId": "a" * 64,
            "exchangeImageId": _image_id(54),
            "integrationDatabasePath": stage_transition.EXCHANGE_INTEGRATION_DATABASE,
            "environmentName": stage_transition.EXCHANGE_CLOUD_RUNNER_ENV,
            "activeComposeValue": "false",
            "candidateComposeValue": "false",
            "liveContainerValue": "false",
            "idleCounts": {
                "cloudRefreshNonterminalCount": 0,
                "cloudRefreshActiveLeaseCount": 0,
                "authorizationMonitorActiveLeaseCount": 0,
                "emailNotificationNonterminalCount": 0,
            },
        },
    }

    rendered, _lock = stage_transition._candidate_configs(plan, ["docker"])
    assert rendered == candidate

    candidate["services"]["exchange"]["environment"][stage_transition.EXCHANGE_CLOUD_RUNNER_ENV] = (
        "true"
    )
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="AuthorizationCloudRunner=false",
    ):
        stage_transition._candidate_configs(plan, ["docker"])


def test_image_update_rejects_config_and_non_target_image_changes(tmp_path: Path) -> None:
    """Keep image-update plans closed to one image reference and ID change."""

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange", tmp_path)
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

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange", tmp_path)
    candidate["services"]["navigator"]["image"] = "navigator:unexpected"
    candidate_lock["images"] = [
        {**row, "image": "navigator:unexpected", "imageId": _image_id(52)}
        if row["service"] == "dsh-executor"
        else row
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

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange", tmp_path)
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

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange", tmp_path)
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

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange", tmp_path)
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


def test_image_update_rejects_unchanged_or_unknown_targets(tmp_path: Path) -> None:
    """Require one real Exchange/Navigator image change and reject other targets."""

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange", tmp_path)
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

    active, candidate, active_lock, candidate_lock = _active_image_update("exchange", tmp_path)
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="must be Cloud Management, Exchange, GitHub Issues, Hugging Face, Navigator, or Persistence",
    ):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            image_update_target="account-center",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_image_update(
        "cloud-management-phase2", tmp_path
    )
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="phase must be provider-bootstrap or issued-connector-activation",
    ):
        stage_transition._validate_phase_topology(
            phase="unsupported-phase",
            image_update_target="cloud-management-phase2",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_image_update(
        "navigator-persistence", tmp_path
    )
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="Persistence image updates require issued-connector-activation",
    ):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            image_update_target="navigator-persistence",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )

    active, candidate, active_lock, candidate_lock = _active_image_update("huggingface", tmp_path)
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="Hugging Face image updates require issued-connector-activation",
    ):
        stage_transition._validate_phase_topology(
            phase="provider-bootstrap",
            image_update_target="huggingface",
            active=active,
            candidate=candidate,
            active_lock=active_lock,
            candidate_lock=candidate_lock,
        )


def test_stage_transition_cli_closes_image_update_target_choices() -> None:
    """Expose only the six services with explicit V2 image-update gates.

    中文：CLI 只开放具备独立严格门禁的 Exchange、GitHub、HF、Navigator、Persistence 与 Cloud 目标。
    """

    parser = stage_transition._build_parser()
    commands = next(action for action in parser._actions if action.dest == "command")
    plan_parser = commands.choices["plan"]
    image_target_action = next(
        action
        for action in plan_parser._actions
        if "--image-update-target" in action.option_strings
    )

    assert image_target_action.choices == [
        "cloud-management-phase2",
        "exchange",
        "github-issues",
        "huggingface",
        "navigator",
        "navigator-persistence",
    ]
    memory_target_action = next(
        action
        for action in plan_parser._actions
        if "--memory-update-target" in action.option_strings
    )
    assert memory_target_action.choices == sorted(stage_transition.MEMORY_UPDATE_TARGETS)
    listener_action = next(
        action for action in plan_parser._actions if "--listener-repair" in action.option_strings
    )
    assert listener_action.dest == "listener_repair"
    assert listener_action.default is False


@pytest.mark.parametrize(
    ("phase", "image_target", "memory_target", "listener_repair"),
    [
        ("issued-connector-activation", "exchange", None, False),
        ("provider-bootstrap", None, None, True),
        ("issued-connector-activation", None, "teams-ingress", False),
    ],
)
def test_stage_transition_cli_passes_image_update_target_to_plan_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    image_target: str | None,
    memory_target: str | None,
    listener_repair: bool,
) -> None:
    """Forward either closed operation selector without inferring it from the phase.

    中文：镜像更新与 listener repair 是独立显式操作，不从 phase 推断。
    """

    captured: dict[str, Any] = {}
    monkeypatch.setattr(runtime_update, "_validate_docker_prefix", lambda _prefix: None)

    def capture_plan(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"operationId": "operation-1", "planSha256": "a" * 64}

    monkeypatch.setattr(stage_transition, "create_plan", capture_plan)
    arguments = ["plan", "--phase", phase]
    if image_target is not None:
        arguments.extend(["--image-update-target", image_target])
    if memory_target is not None:
        arguments.extend(["--memory-update-target", memory_target])
    if listener_repair:
        arguments.append("--listener-repair")
    arguments.extend(
        [
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
    )

    assert stage_transition.main(arguments) == 0
    assert captured["phase"] == phase
    assert captured["image_update_target"] == image_target
    assert captured["memory_update_target"] == memory_target
    assert captured["listener_repair"] is listener_repair


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
    stage_transition._validate_image_update_selection_contract(
        target="github-issues",
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
    stage_transition._validate_image_update_selection_contract(
        target="huggingface",
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
    stage_transition._validate_image_update_selection_contract(
        target="navigator-persistence",
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

    bootstrap_selection = copy.deepcopy(selection)
    bootstrap_selection["document"]["phase"] = "provider-bootstrap"
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="GitHub Issues image updates require issued-connector-activation",
    ):
        stage_transition._validate_image_update_selection_contract(
            target="github-issues",
            phase="provider-bootstrap",
            project="cyrene-teams-cloud",
            selection_record=bootstrap_selection,
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


def test_memory_update_requires_same_selector_env_lock_and_mount_identity() -> None:
    """Bind a hot memory update to the active selector and identical runtime inputs."""

    active_inputs, candidate_inputs, selection, identity = _image_update_selection_inputs()
    candidate_inputs["imageLock"] = copy.deepcopy(active_inputs["imageLock"])
    stage_transition._validate_image_update_selection_contract(
        target=None,
        memory_update_target="teams-ingress",
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

    changed_env = copy.deepcopy(candidate_inputs)
    changed_env["envFiles"][0]["sha256"] = "9" * 64
    with pytest.raises(stage_transition.StageTransitionError, match="env files and image lock"):
        stage_transition._validate_image_update_selection_contract(
            target=None,
            memory_update_target="teams-ingress",
            phase="issued-connector-activation",
            project="cyrene-teams-cloud",
            selection_record=selection,
            selection_path="/private/runtime-selection.json",
            active_config_dir="/private/config",
            candidate_config_dir="/private/config",
            active_secrets_dir="/private/secrets",
            active_inputs=active_inputs,
            candidate_inputs=changed_env,
            runtime_config_identity=identity,
        )

    changed_lock = copy.deepcopy(candidate_inputs)
    changed_lock["imageLock"]["sha256"] = "8" * 64
    with pytest.raises(stage_transition.StageTransitionError, match="env files and image lock"):
        stage_transition._validate_image_update_selection_contract(
            target=None,
            memory_update_target="teams-ingress",
            phase="issued-connector-activation",
            project="cyrene-teams-cloud",
            selection_record=selection,
            selection_path="/private/runtime-selection.json",
            active_config_dir="/private/config",
            candidate_config_dir="/private/config",
            active_secrets_dir="/private/secrets",
            active_inputs=active_inputs,
            candidate_inputs=changed_lock,
            runtime_config_identity=identity,
        )


def _listener_repair_selection_inputs() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
]:
    """Build one bootstrap selector and candidate command-source record for repair tests."""

    active_compose = [{"path": "/private/active-compose.yaml", "sha256": "a" * 64}]
    candidate_compose = [{"path": "/private/candidate-compose.yaml", "sha256": "b" * 64}]
    env_files = [{"path": "/private/runtime.env", "sha256": "c" * 64}]
    image_lock = {"path": "/private/images.json", "sha256": "d" * 64}
    config_files = [{"path": "navigator-principal-config.json", "sha256": "e" * 64}]
    secret_files = [{"name": "exchange-hmac", "sha256": "f" * 64}]
    seed = {"path": "/private/huggingface-oauth.json", "sha256": "1" * 64}
    mount_identity = {"navigator": []}
    active_inputs = {
        "composeFiles": active_compose,
        "envFiles": env_files,
        "imageLock": image_lock,
        "configDirectoryFiles": config_files,
        "secretFiles": secret_files,
    }
    candidate_inputs = {
        "composeFiles": candidate_compose,
        "envFiles": env_files,
        "imageLock": image_lock,
        "configDirectoryFiles": config_files,
        "secretFiles": secret_files,
        "huggingfaceSeed": seed,
        "runpodKey": None,
    }
    selection = {
        "schemaVersion": stage_transition.SELECTION_SCHEMA,
        "project": "cyrene-teams-cloud",
        "operationId": "00000000-0000-4000-8000-000000000001",
        "planSha256": "2" * 64,
        "phase": "provider-bootstrap",
        "composeFiles": active_compose,
        "envFiles": env_files,
        "imageLock": image_lock,
        "secretsDir": "/private/secrets",
        "configDir": "/private/config",
        "huggingfaceSeed": seed,
        "runpodKey": None,
        "runtimeConfigIdentity": mount_identity,
        "updatedAtUtc": "2026-10-26T12:00:00+00:00",
    }
    selection_record = {
        "path": "/private/runtime-selection.json",
        "sha256": "3" * 64,
        "document": selection,
    }
    runtime_identity = {"active": mount_identity, "candidate": copy.deepcopy(mount_identity)}
    return active_inputs, candidate_inputs, selection_record, runtime_identity


def test_listener_repair_requires_the_current_bootstrap_selector_and_unchanged_inputs() -> None:
    """Keep listener repair on selector-v2 with identical env, lock, config, and mounts."""

    active_inputs, candidate_inputs, selection, identity = _listener_repair_selection_inputs()
    kwargs = {
        "phase": "provider-bootstrap",
        "project": "cyrene-teams-cloud",
        "selection_record": selection,
        "selection_path": "/private/runtime-selection.json",
        "active_config_dir": "/private/config",
        "candidate_config_dir": "/private/config",
        "active_secrets_dir": "/private/secrets",
        "active_inputs": active_inputs,
        "candidate_inputs": candidate_inputs,
        "runtime_config_identity": identity,
    }
    stage_transition._validate_listener_repair_selection_contract(**kwargs)

    changed_env = copy.deepcopy(kwargs)
    changed_env["candidate_inputs"] = copy.deepcopy(candidate_inputs)
    changed_env["candidate_inputs"]["envFiles"][0]["sha256"] = "9" * 64
    with pytest.raises(stage_transition.StageTransitionError, match="env and image lock inputs"):
        stage_transition._validate_listener_repair_selection_contract(**changed_env)

    changed_lock = copy.deepcopy(kwargs)
    changed_lock["candidate_inputs"] = copy.deepcopy(candidate_inputs)
    changed_lock["candidate_inputs"]["imageLock"]["sha256"] = "8" * 64
    with pytest.raises(stage_transition.StageTransitionError, match="env and image lock inputs"):
        stage_transition._validate_listener_repair_selection_contract(**changed_lock)

    changed_config = copy.deepcopy(kwargs)
    changed_config["candidate_inputs"] = copy.deepcopy(candidate_inputs)
    changed_config["candidate_inputs"]["configDirectoryFiles"][0]["sha256"] = "7" * 64
    with pytest.raises(stage_transition.StageTransitionError, match="every config, secret"):
        stage_transition._validate_listener_repair_selection_contract(**changed_config)

    changed_phase = copy.deepcopy(kwargs)
    changed_phase["phase"] = "issued-connector-activation"
    with pytest.raises(stage_transition.StageTransitionError, match="provider-bootstrap selector"):
        stage_transition._validate_listener_repair_selection_contract(**changed_phase)


@pytest.mark.parametrize(
    ("target", "expected", "strict_old"),
    [
        ("exchange", "old", True),
        ("cloud-management-phase2", "old", True),
        ("huggingface", "old", True),
        ("navigator-persistence", "old", True),
        ("navigator", "fresh", False),
        (None, "fresh", False),
    ],
)
def test_image_update_resumes_old_executor_or_fresh_navigator_by_target(
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
    expected: str,
    strict_old: bool,
) -> None:
    """Non-Navigator image updates fence the old generation; Navigator uses the fresh gate.

    中文：更新 Exchange、Cloud、HF 或 Persistence 时沿用原实例；Navigator 镜像更新走新实例门。
    """

    observed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        stage_transition,
        "_resume_old_executor_if_needed",
        lambda **kwargs: observed.append(("old", kwargs.get("require_original_instance") is True)),
    )
    monkeypatch.setattr(
        stage_transition,
        "_resume_live_navigator",
        lambda **_kwargs: observed.append(("fresh", False)),
    )

    stage_transition._resume_executor_after_transition(
        plan={"imageUpdateTarget": target} if target is not None else {},
        journal={},
        candidate_compose={},
        docker_prefix=["docker"],
    )

    assert observed == [(expected, strict_old)]


def test_resume_old_executor_accepts_only_the_planned_navigator_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already accepting old process must retain its exact container and generation."""

    container_id = "a" * 64
    instance_id = "11111111-1111-4111-8111-111111111111"
    initial_generation = 4
    drain_generation = initial_generation + 1
    expected_attempt = {
        "containerId": container_id,
        "instanceId": instance_id,
        "initialGeneration": initial_generation,
        "generation": drain_generation,
    }
    observed_resume_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_list_project_navigator_containers",
        lambda **_kwargs: [{"containerId": container_id, "running": True}],
    )
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            "mode": "accepting",
            "instanceId": instance_id,
            "generation": initial_generation,
        },
    )
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_resume_navigator_instance",
        lambda **kwargs: observed_resume_calls.append(kwargs),
    )

    journal = {
        "navigator": {
            "initialContainerId": container_id,
            "initialInstanceId": instance_id,
            "initialGeneration": initial_generation,
            "drainAttempt": None,
            "drainGeneration": None,
        },
        "targetProgress": {},
    }
    stage_transition._resume_old_executor_if_needed(
        plan={"project": "cyrene-teams-cloud"},
        journal=journal,
        docker_prefix=["docker"],
        require_original_instance=True,
    )
    assert observed_resume_calls == []

    journal["navigator"]["drainAttempt"] = expected_attempt
    journal["navigator"]["drainGeneration"] = drain_generation
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            "mode": "accepting",
            "instanceId": instance_id,
            "generation": drain_generation,
        },
    )
    stage_transition._resume_old_executor_if_needed(
        plan={"project": "cyrene-teams-cloud"},
        journal=journal,
        docker_prefix=["docker"],
        require_original_instance=True,
    )
    assert observed_resume_calls == []


@pytest.mark.parametrize(
    ("running_container_id", "instance_id", "generation", "drain_attempt", "drain_generation"),
    [
        ("b" * 64, "11111111-1111-4111-8111-111111111111", 4, None, None),
        ("a" * 64, "22222222-2222-4222-8222-222222222222", 4, None, None),
        ("a" * 64, "11111111-1111-4111-8111-111111111111", 99, None, None),
        ("a" * 64, "11111111-1111-4111-8111-111111111111", 5, None, None),
        (
            "a" * 64,
            "11111111-1111-4111-8111-111111111111",
            4,
            {
                "containerId": "a" * 64,
                "instanceId": "11111111-1111-4111-8111-111111111111",
                "initialGeneration": 4,
                "generation": 5,
            },
            5,
        ),
    ],
)
def test_resume_old_executor_rejects_unknown_accepting_identity(
    monkeypatch: pytest.MonkeyPatch,
    running_container_id: str,
    instance_id: str,
    generation: int,
    drain_attempt: dict[str, object] | None,
    drain_generation: int | None,
) -> None:
    """An unrelated accepting container, process, or generation cannot pass recovery."""

    container_id = "a" * 64
    initial_instance_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_list_project_navigator_containers",
        lambda **_kwargs: [{"containerId": running_container_id, "running": True}],
    )
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            "mode": "accepting",
            "instanceId": instance_id,
            "generation": generation,
        },
    )
    journal = {
        "navigator": {
            "initialContainerId": container_id,
            "initialInstanceId": initial_instance_id,
            "initialGeneration": 4,
            "drainAttempt": drain_attempt,
            "drainGeneration": drain_generation,
        },
        "targetProgress": {},
    }

    with pytest.raises(stage_transition.StageTransitionError):
        stage_transition._resume_old_executor_if_needed(
            plan={"project": "cyrene-teams-cloud"},
            journal=journal,
            docker_prefix=["docker"],
            require_original_instance=True,
        )


def test_resume_old_executor_keeps_recorded_two_phase_rollback_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal two-phase rollback may use its explicitly recorded old-image container."""

    initial_container_id = "a" * 64
    rollback_container_id = "b" * 64
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_list_project_navigator_containers",
        lambda **_kwargs: [{"containerId": rollback_container_id, "running": True}],
    )
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            "mode": "accepting",
            "instanceId": "22222222-2222-4222-8222-222222222222",
            "generation": 0,
        },
    )
    journal = {
        "navigator": {
            "initialContainerId": initial_container_id,
            "initialInstanceId": "11111111-1111-4111-8111-111111111111",
            "initialGeneration": 0,
            "drainAttempt": None,
            "drainGeneration": None,
        },
        "targetProgress": {
            "navigator": {
                "phase": "ROLLED_BACK",
                "rollbackContainerId": rollback_container_id,
            }
        },
    }

    stage_transition._resume_old_executor_if_needed(
        plan={"project": "cyrene-teams-cloud"},
        journal=journal,
        docker_prefix=["docker"],
    )


def test_resume_old_executor_resumes_only_the_journaled_drain_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draining process resumes only when its container, instance, and attempt match."""

    container_id = "a" * 64
    instance_id = "11111111-1111-4111-8111-111111111111"
    initial_generation = 4
    drain_generation = initial_generation + 1
    expected_attempt = {
        "containerId": container_id,
        "instanceId": instance_id,
        "initialGeneration": initial_generation,
        "generation": drain_generation,
    }
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_list_project_navigator_containers",
        lambda **_kwargs: [{"containerId": container_id, "running": True}],
    )
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_current_navigator_status",
        lambda *_args, **_kwargs: {
            "mode": "draining",
            "instanceId": instance_id,
            "generation": drain_generation,
        },
    )
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_resume_navigator_instance",
        lambda **kwargs: observed.append(kwargs),
    )
    journal = {
        "navigator": {
            "initialContainerId": container_id,
            "initialInstanceId": instance_id,
            "initialGeneration": initial_generation,
            "drainAttempt": expected_attempt,
            "drainGeneration": drain_generation,
        },
        "targetProgress": {},
    }

    stage_transition._resume_old_executor_if_needed(
        plan={"project": "cyrene-teams-cloud"},
        journal=journal,
        docker_prefix=["docker"],
        require_original_instance=True,
    )

    assert observed == [
        {
            "docker_prefix": ["docker"],
            "container_id": container_id,
            "instance_id": instance_id,
            "generation": drain_generation,
            "timeout_seconds": 30,
        }
    ]


@pytest.mark.parametrize(
    ("phase", "target", "required"),
    [
        ("provider-bootstrap", "exchange", True),
        ("provider-bootstrap", "cloud-management-phase2", True),
        ("provider-bootstrap", "navigator", False),
        ("issued-connector-activation", "exchange", True),
        ("issued-connector-activation", "cloud-management-phase2", True),
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


def _recovered_principal_config_pair(
    tmp_path: Path,
    *,
    alias: str = stage_transition.RECOVERED_PERSISTENCE_PRINCIPAL_CONFIG,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    """Build the active/candidate config alias used for Persistence's principal file."""

    active_dir = tmp_path / "active-config"
    candidate_dir = tmp_path / "candidate-config"
    active_dir.mkdir()
    candidate_dir.mkdir()
    active_file = active_dir / stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
    candidate_file = candidate_dir / stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
    active_file.write_text('{"principals":[]}', encoding="utf-8")
    candidate_file.write_text(
        '{"principals":[{"can_publish_credential_health":true}]}', encoding="utf-8"
    )
    active_configs = {
        "navigator-principal": {"file": str(active_file)},
        alias: {"file": str(active_file)},
    }
    candidate_configs = {
        "navigator-principal": {"file": str(candidate_file)},
        alias: {"file": str(candidate_file)},
    }
    active = {
        "configs": active_configs,
        "services": {
            "navigator-persistence": {
                "configs": [
                    {
                        "source": alias,
                        "target": stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_TARGET,
                    }
                ]
            }
        },
    }
    candidate = copy.deepcopy(active)
    candidate["configs"] = candidate_configs
    return active_dir, candidate_dir, active, candidate


def test_recovered_persistence_principal_alias_allows_only_its_scoped_content_update(
    tmp_path: Path,
) -> None:
    """Allow the existing owner-principal alias only at its unique Persistence target."""

    active_dir, candidate_dir, active, candidate = _recovered_principal_config_pair(tmp_path)

    stage_transition._validate_active_candidate_configs(
        active_dir,
        candidate_dir,
        active,
        candidate,
    )


@pytest.mark.parametrize(
    "scope_change",
    ["service", "target", "duplicate", "basename", "active_directory", "directory", "alias"],
)
def test_recovered_persistence_principal_alias_rejects_wrong_consumer_or_source(
    tmp_path: Path,
    scope_change: str,
) -> None:
    """Reject principal alias changes outside the exact candidate config path and mount."""

    alias = (
        "recovered-other-principal"
        if scope_change == "alias"
        else stage_transition.RECOVERED_PERSISTENCE_PRINCIPAL_CONFIG
    )
    active_dir, candidate_dir, active, candidate = _recovered_principal_config_pair(
        tmp_path,
        alias=alias,
    )
    if scope_change == "service":
        candidate["services"]["navigator"] = copy.deepcopy(
            candidate["services"].pop("navigator-persistence")
        )
    elif scope_change == "target":
        candidate["services"]["navigator-persistence"]["configs"][0]["target"] = (
            "/run/secrets/other-principal.json"
        )
    elif scope_change == "duplicate":
        candidate["services"]["navigator"] = {}
        candidate["services"]["navigator"]["configs"] = [
            {
                "source": alias,
                "target": stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_TARGET,
            }
        ]
    elif scope_change == "basename":
        wrong_file = candidate_dir / "different-principal.json"
        wrong_file.write_text("candidate principal", encoding="utf-8")
        candidate["configs"][alias]["file"] = str(wrong_file)
    elif scope_change == "active_directory":
        outside_dir = tmp_path / "outside-active-config"
        outside_dir.mkdir()
        outside_file = outside_dir / stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
        outside_file.write_text('{"principals":[{"unexpected":true}]}', encoding="utf-8")
        active["configs"][alias]["file"] = str(outside_file)
    elif scope_change == "directory":
        outside_dir = tmp_path / "outside-config"
        outside_dir.mkdir()
        outside_file = outside_dir / stage_transition.NAVIGATOR_PRINCIPAL_CONFIG_FILENAME
        outside_file.write_text(
            '{"principals":[{"can_publish_credential_health":true}]}', encoding="utf-8"
        )
        candidate["configs"][alias]["file"] = str(outside_file)

    with pytest.raises(stage_transition.StageTransitionError):
        stage_transition._validate_active_candidate_configs(
            active_dir,
            candidate_dir,
            active,
            candidate,
        )


def _runpod_notification_config_pair(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    """Build an active notification overlay and a RunPod-only candidate update."""

    active_dir = tmp_path / "active-config"
    candidate_dir = tmp_path / "candidate-config"
    active_dir.mkdir()
    candidate_dir.mkdir()
    active_notification = {
        "schemaVersion": 1,
        "organizationId": "00000000-0000-4000-8000-000000000001",
        "workspaceId": "00000000-0000-4000-8000-000000000002",
        "owner": {"subject": "00000000-0000-4000-8000-000000000003"},
        "connections": [
            {"provider": provider, "connectionId": f"00000000-0000-4000-8000-{number:012x}"}
            for number, provider in enumerate(("hf", "azure", "gcp"), start=10)
        ],
        "destination": {
            "teamId": "team-a",
            "channelId": "channel-a",
            "rootThreadId": "12345",
        },
        "exchange": {"profileRef": "teams-notification-profile"},
        "pollIntervalMs": 300000,
    }
    runpod_connection = "00000000-0000-4000-8000-000000000020"
    candidate_notification = copy.deepcopy(active_notification)
    candidate_notification["connections"].append(
        {"provider": "runpod", "connectionId": runpod_connection}
    )
    (active_dir / "teams-credential-notifications.json").write_text(
        json.dumps(active_notification), encoding="utf-8"
    )
    (candidate_dir / "teams-credential-notifications.json").write_text(
        json.dumps(candidate_notification), encoding="utf-8"
    )
    active_bindings = [
        {
            "bindingId": f"00000000-0000-4000-8000-{number + 100:012x}",
            "connectionRef": row["connectionId"],
            "providerId": {"hf": "huggingface", "azure": "azure", "gcp": "gcp"}[row["provider"]],
            "profileRef": f"{row['provider']}-profile",
            "tools": [
                {
                    "toolId": "readonly-tool",
                    "operationId": "readonly.operation",
                    "resourceProjection": "profile",
                    "mutating": False,
                }
            ],
        }
        for number, row in enumerate(active_notification["connections"])
    ]
    runpod_tools = [
        "cloud.provider.health.read",
        "cloud.resources.list",
        "cloud.resource.get",
        "cloud.operation.status.read",
        "cloud.operation.logs.read",
    ]
    runpod_binding = {
        "bindingId": "00000000-0000-4000-8000-000000000120",
        "connectionRef": runpod_connection,
        "providerId": "runpod",
        "profileRef": "runpod-issued-profile",
        "tools": [
            {
                "toolId": operation_id,
                "operationId": operation_id,
                "resourceProjection": "profile",
                "mutating": False,
            }
            for operation_id in runpod_tools
        ],
    }
    (active_dir / "connector-bindings.json").write_text(
        json.dumps({"bindings": active_bindings}), encoding="utf-8"
    )
    (candidate_dir / "connector-bindings.json").write_text(
        json.dumps({"bindings": active_bindings + [runpod_binding]}), encoding="utf-8"
    )
    active = {
        "configs": {
            "teams-credential-notifications": {
                "file": str(active_dir / "teams-credential-notifications.json")
            },
            "connector-bindings": {"file": str(active_dir / "connector-bindings.json")},
        },
        "services": {
            "cloud-management-phase2": {
                "environment": {
                    "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
                        {"profiles": [{"provider": "azure"}, {"provider": "gcp"}]}
                    )
                }
            }
        },
    }
    candidate = copy.deepcopy(active)
    candidate["configs"]["teams-credential-notifications"]["file"] = str(
        candidate_dir / "teams-credential-notifications.json"
    )
    candidate["configs"]["connector-bindings"]["file"] = str(
        candidate_dir / "connector-bindings.json"
    )
    candidate["services"]["cloud-management-phase2"]["environment"][
        "CYRENE_CAPABILITY_CONFIGURATION_JSON"
    ] = json.dumps(
        {
            "profiles": [
                {"provider": "azure"},
                {"provider": "gcp"},
                {
                    "provider": "runpod",
                    "profile_ref": "runpod-primary",
                    "identity_ref": "runpod-identity",
                    "auth": {"mode": "api_key"},
                    "read_only": True,
                },
            ]
        }
    )
    return active_dir, candidate_dir, active, candidate


def test_active_notification_config_allows_only_the_scoped_runpod_addition(
    tmp_path: Path,
) -> None:
    """Allow an already-enabled overlay to add one read-only RunPod notification binding."""

    active_dir, candidate_dir, active, candidate = _runpod_notification_config_pair(tmp_path)

    stage_transition._validate_active_candidate_configs(
        active_dir,
        candidate_dir,
        active,
        candidate,
    )


@pytest.mark.parametrize("mutation", ["route", "existing_connection", "write_binding"])
def test_active_notification_config_rejects_unrelated_or_mutating_runpod_updates(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Reject notification scope drift and any write or non-allowlisted RunPod binding."""

    active_dir, candidate_dir, active, candidate = _runpod_notification_config_pair(tmp_path)
    candidate_notification_path = candidate_dir / "teams-credential-notifications.json"
    candidate_notification = json.loads(candidate_notification_path.read_text(encoding="utf-8"))
    candidate_binding_path = candidate_dir / "connector-bindings.json"
    candidate_binding_document = json.loads(candidate_binding_path.read_text(encoding="utf-8"))
    runpod_binding = next(
        row for row in candidate_binding_document["bindings"] if row["providerId"] == "runpod"
    )
    if mutation == "route":
        candidate_notification["destination"]["channelId"] = "other-channel"
        candidate_notification_path.write_text(json.dumps(candidate_notification), encoding="utf-8")
    elif mutation == "existing_connection":
        candidate_notification["connections"][0]["connectionId"] = (
            "00000000-0000-4000-8000-000000000099"
        )
        candidate_notification_path.write_text(json.dumps(candidate_notification), encoding="utf-8")
    else:
        runpod_binding["tools"].append(
            {
                "toolId": "cloud.resource.create",
                "operationId": "cloud.resource.create",
                "resourceProjection": "profile",
                "mutating": True,
            }
        )
        candidate_binding_path.write_text(json.dumps(candidate_binding_document), encoding="utf-8")

    with pytest.raises(stage_transition.StageTransitionError):
        stage_transition._validate_active_candidate_configs(
            active_dir,
            candidate_dir,
            active,
            candidate,
        )


def test_notification_config_addition_is_expected_only_when_overlay_is_inactive(
    tmp_path: Path,
) -> None:
    """Keep the one allowed config addition limited to the first overlay activation."""

    active_dir = tmp_path / "active-config"
    candidate_dir = tmp_path / "candidate-config"
    active_dir.mkdir()
    candidate_dir.mkdir()
    candidate_notification = candidate_dir / "teams-credential-notifications.json"
    candidate_notification.write_text("{}", encoding="utf-8")
    active = {"configs": {}}
    candidate = {
        "configs": {"teams-credential-notifications": {"file": str(candidate_notification)}}
    }
    stage_transition._validate_active_candidate_configs(
        active_dir,
        candidate_dir,
        active,
        candidate,
    )


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


def test_activation_is_config_only_and_keeps_every_immutable_image(tmp_path: Path) -> None:
    """Issued connector activation cannot silently rebuild or upgrade a service image."""

    active, _unused_candidate, candidate_lock, _unused_lock = _listener_repair_pair(tmp_path)
    candidate = copy.deepcopy(active)
    active["services"]["navigator"]["environment"] = {}
    candidate_lock = copy.deepcopy(candidate_lock)
    targets, _images = stage_transition._validate_phase_topology(
        phase="issued-connector-activation",
        active=active,
        candidate=candidate,
        active_lock=candidate_lock,
        candidate_lock=candidate_lock,
    )
    assert "navigator" in targets

    changed_lock = copy.deepcopy(candidate_lock)
    cloud_row = next(
        row
        for row in changed_lock["images"]
        if row["service"] == runtime_update.LOCK_SERVICE_FOR_TARGET["cloud-management-phase2"]
    )
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


def test_persistence_image_update_requires_identical_schema_and_all_rows() -> None:
    """Image-only Persistence accepts exact state and rejects additive schema or row drift."""

    before = _sqlite_report_fixture(
        {"sessions": _table_report(name="sessions", row_count=1, rows_digest="a" * 64)}
    )
    unchanged = copy.deepcopy(before)
    stage_transition._validate_persistence_same_schema_reports(before, unchanged)
    assert unchanged["migrationCompatibility"]["class"] == ("SAME_SCHEMA_AND_ALL_ROWS_PRESERVED")

    changed_rows = copy.deepcopy(before)
    changed_rows["tables"]["sessions"]["rowsDigest"] = "b" * 64
    with pytest.raises(stage_transition.StageTransitionError, match="rowsDigest"):
        stage_transition._validate_persistence_same_schema_reports(before, changed_rows)

    changed_schema = _sqlite_report_fixture(
        {
            "sessions": _table_report(name="sessions", row_count=1, rows_digest="a" * 64),
            "unexpected": _table_report(name="unexpected", row_count=0, rows_digest="c" * 64),
        }
    )
    with pytest.raises(stage_transition.StageTransitionError, match="objects"):
        stage_transition._validate_persistence_same_schema_reports(before, changed_schema)


def test_persistence_active_writer_lease_readback_is_count_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read only the active writer count and reject malformed database reports."""

    observed: list[list[str]] = []
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_checked_stdout",
        lambda argv, **kwargs: observed.append(argv) or '{"activeWriterLeaseCount":0}',
    )
    assert (
        stage_transition._active_persistence_writer_lease_count(
            docker_prefix=["docker"],
            container_id="a" * 64,
            database_path="/var/lib/cyrene-navigator/persistence.db",
        )
        == 0
    )
    assert observed[0][:4] == ["docker", "exec", "a" * 64, "/opt/venv/bin/python"]
    assert "SELECT COUNT(*) FROM sessions" in observed[0][5]

    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: '{"activeWriterLeaseCount":1}',
    )
    assert (
        stage_transition._active_persistence_writer_lease_count(
            docker_prefix=["docker"],
            container_id="a" * 64,
            database_path="/var/lib/cyrene-navigator/persistence.db",
        )
        == 1
    )


def test_persistence_quiescence_gate_requires_stable_database_and_no_writer_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist the gate only after a fresh exact-data read and zero active writer count."""

    baseline = _sqlite_report_fixture(
        {"sessions": _table_report(name="sessions", row_count=1, rows_digest="a" * 64)}
    )
    plan = {
        "activeServices": {"navigator-persistence": {"containerId": "a" * 64}},
        "database": {"containerPath": "/var/lib/cyrene-navigator/persistence.db"},
    }
    phases: list[str] = []

    monkeypatch.setattr(
        stage_transition,
        "_live_sqlite_report",
        lambda **_kwargs: copy.deepcopy(baseline),
    )
    monkeypatch.setattr(stage_transition, "_active_persistence_writer_lease_count", lambda **_: 0)
    monkeypatch.setattr(
        stage_transition,
        "_write_journal",
        lambda _path, _journal, phase: phases.append(phase),
    )
    journal = {"database": {"snapshot": copy.deepcopy(baseline)}}
    stage_transition._verify_persistence_update_quiescence(
        plan=plan,
        journal=journal,
        journal_path=tmp_path / "journal.json",
        docker_prefix=["docker"],
    )
    assert phases == ["PERSISTENCE_SAME_SCHEMA_QUIESCENCE_VERIFIED"]
    assert journal["database"]["preUpdateQuiescence"]["activeWriterLeaseCount"] == 0

    phases.clear()
    monkeypatch.setattr(stage_transition, "_active_persistence_writer_lease_count", lambda **_: 1)
    with pytest.raises(
        stage_transition.StageTransitionError, match="zero unexpired Session writer"
    ):
        stage_transition._verify_persistence_update_quiescence(
            plan=plan,
            journal={"database": {"snapshot": copy.deepcopy(baseline)}},
            journal_path=tmp_path / "journal.json",
            docker_prefix=["docker"],
        )
    assert phases == []


def test_persistence_image_update_mutation_blocks_automatic_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once replacement intent is journaled, recovery never restores an older image."""

    journal = {
        "state": "RUNNING",
        "lastErrorCode": None,
        "targetProgress": {
            "navigator-persistence": {
                "phase": "APPLIED",
                "applyIntent": False,
                "candidateContainerId": "b" * 64,
            }
        },
    }
    assert not stage_transition._persistence_image_update_may_have_started(
        {"imageUpdateTarget": "navigator-persistence"},
        {
            "targetProgress": {
                "navigator-persistence": {
                    "phase": "PREPARED",
                    "applyIntent": False,
                    "candidateContainerId": None,
                }
            }
        },
    )

    def record(
        _path: Path, document: dict[str, Any], _phase: str, *, state: str | None = None
    ) -> None:
        if state is not None:
            document["state"] = state

    monkeypatch.setattr(stage_transition, "_write_journal", record)
    monkeypatch.setattr(
        stage_transition.runtime_update,
        "_load_lock",
        lambda *_args, **_kwargs: pytest.fail("Persistence rollback must not load the old lock"),
    )
    with pytest.raises(stage_transition.StageTransitionError, match="may have started"):
        stage_transition._rollback_known_mutations(
            plan={
                "imageUpdateTarget": "navigator-persistence",
                "activeInputs": {"imageLock": {"path": "/private/active-lock.json"}},
            },
            active_compose={"services": {}},
            journal=journal,
            journal_path=tmp_path / "journal.json",
            docker_prefix=["docker"],
        )
    assert journal["state"] == "RECOVERY_REQUIRED"
    assert journal["lastErrorCode"] == "persistence_update_requires_manual_reconciliation"


def test_stage_transition_summary_reports_persistence_gate_evidence() -> None:
    """Expose quiescence and exact-row proof through the normal apply/status result."""

    summary = stage_transition._journal_summary(
        {
            "schemaVersion": stage_transition.JOURNAL_SCHEMA,
            "operationId": "00000000-0000-4000-8000-000000000001",
            "planSha256": "a" * 64,
            "project": "cyrene-teams-cloud",
            "state": "APPLIED",
            "phase": "APPLIED",
            "targets": ["navigator-persistence"],
            "completedTargets": ["navigator-persistence"],
            "updatedAtUtc": "2026-10-27T00:00:00+00:00",
            "database": {
                "backupSha256": "b" * 64,
                "snapshot": {"userVersion": 0},
                "preUpdateQuiescence": {
                    "verified": True,
                    "activeWriterLeaseCount": 0,
                },
                "postMigration": {
                    "migrationCompatibility": {"class": "SAME_SCHEMA_AND_ALL_ROWS_PRESERVED"}
                },
            },
            "exchangeReadiness": {"required": True, "verified": True},
            "cloudRunnerIdle": {"required": False},
            "listenerRepair": {"required": False},
            "selection": {"promoted": True},
            "lastErrorCode": None,
        }
    )

    assert summary["persistenceQuiescenceStatus"] == "VERIFIED"
    assert summary["persistenceSameSchemaStatus"] == "VERIFIED"


def test_stage_transition_summary_reports_huggingface_restart_gates() -> None:
    """Expose the before/after readiness and monitor window in normal operation status."""

    journal = {
        "schemaVersion": stage_transition.JOURNAL_SCHEMA,
        "operationId": "00000000-0000-4000-8000-000000000001",
        "planSha256": "a" * 64,
        "project": "cyrene-teams-cloud",
        "state": "APPLIED",
        "phase": "APPLIED",
        "targets": ["huggingface"],
        "completedTargets": ["huggingface"],
        "updatedAtUtc": "2026-10-27T00:00:00+00:00",
        "exchangeReadiness": {"required": True, "verified": True},
        "huggingfaceImageUpdate": {
            "required": True,
            "beforeReadiness": {"verified": True},
            "monitorQuiescence": {"verified": True},
            "afterReadiness": {"verified": True},
        },
        "cloudRunnerIdle": {"required": False},
        "listenerRepair": {"required": False},
        "selection": {"promoted": True},
    }

    summary = stage_transition._journal_summary(journal)
    assert summary["huggingfaceImageUpdateStatus"] == "VERIFIED"
    assert summary["huggingfaceMonitorQuiescenceStatus"] == "VERIFIED"
    assert summary["huggingfaceBeforeReadinessStatus"] == "LIVE_VERIFIED"
    assert summary["huggingfaceAfterReadinessStatus"] == "LIVE_VERIFIED"

    journal["huggingfaceImageUpdate"]["monitorQuiescence"]["verified"] = False
    blocked = stage_transition._journal_summary(journal)
    assert blocked["huggingfaceMonitorQuiescenceStatus"] == "BLOCKED"
    assert blocked["huggingfaceImageUpdateStatus"] == "BLOCKED"


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


def _listener_repair_readiness_fixture(
    tmp_path: Path, *, include_live_catalog: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the exact frozen 11 plus existing issued HF/Azure/GCP metadata fixture."""

    snapshot, compose = _exchange_readiness_fixture(tmp_path)
    base_bindings: list[dict[str, Any]] = []
    base_catalog: list[dict[str, Any]] = []
    base_connections: list[dict[str, Any]] = []
    repository_scope = []
    for index in range(10):
        connection_ref = f"10000000-0000-4000-8000-{index + 1:012d}"
        repository_id = f"github-repository-{index}"
        owner = "example-org"
        name = f"repository-{index}"
        repository_scope.append({"repositoryId": repository_id, "owner": owner, "name": name})
        binding_id = f"exchange-github-{connection_ref}"
        tool = {
            "toolId": "github.repository.list",
            "operationId": "github.repository.list",
            "resourceProjection": "profile",
            "mutating": False,
        }
        base_bindings.append(
            {
                "bindingId": binding_id,
                "connectionRef": connection_ref,
                "providerId": "github",
                "profileRef": "github-profile",
                "tools": [tool],
            }
        )
        base_catalog.append(
            {
                "bindingId": binding_id,
                "capabilityId": "tool.provider.v1",
                "providerId": "github",
                "connectionRef": connection_ref,
                "profileRef": "github-profile",
                "tools": [
                    {
                        "id": tool["toolId"],
                        "name": "List repositories",
                        "description": "List repositories visible to the issued GitHub profile.",
                        "readOnly": True,
                        "inputSchema": {"type": "object", "additionalProperties": False},
                    }
                ],
            }
        )
        base_connections.append(
            {
                "connectionRef": connection_ref,
                "providerId": "github",
                "profileRef": "github-profile",
                "profileAlias": None,
                "targetAlias": None,
                "accessMode": "workspace_operator",
                "readOnly": False,
                "targetScope": {
                    "repositoryId": repository_id,
                    "repository": f"{owner}/{name}",
                },
            }
        )

    computer_connection = "20000000-0000-4000-8000-000000000001"
    computer_binding_id = f"exchange-computer-{computer_connection}"
    computer_tool = {
        "toolId": "computer.directory.list",
        "operationId": "computer.directory.list",
        "resourceProjection": "computer_directory",
        "mutating": False,
    }
    base_bindings.append(
        {
            "bindingId": computer_binding_id,
            "connectionRef": computer_connection,
            "providerId": "computer",
            "profileRef": "computer-profile",
            "tools": [computer_tool],
        }
    )
    base_catalog.append(
        {
            "bindingId": computer_binding_id,
            "capabilityId": "tool.provider.v1",
            "providerId": "computer",
            "connectionRef": computer_connection,
            "profileRef": "computer-profile",
            "tools": [
                {
                    "id": computer_tool["toolId"],
                    "name": "List workspace directory",
                    "description": "List bounded entries in the authorized workspace.",
                    "readOnly": True,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
    )
    base_connections.append(
        {
            "connectionRef": computer_connection,
            "providerId": "computer",
            "profileRef": "computer-profile",
            "profileAlias": "computer-profile",
            "accessMode": "workspace_operator",
            "readOnly": False,
        }
    )
    base_profiles = [
        {
            "profileRef": "github-profile",
            "profileAlias": None,
            "providerId": "github",
            "authStatus": "healthy",
            "authMode": "github_app",
            "accessMode": "workspace_operator",
            "targetScope": {"repositories": repository_scope},
        },
        {
            "profileRef": "computer-profile",
            "profileAlias": "computer-profile",
            "providerId": "computer",
            "accessMode": "workspace_operator",
            "targetScope": {
                "maxCommandTimeoutMs": 30000,
                "maxFileBytes": 1048576,
                "maxReadBytes": 1048576,
                "maxDirectoryDepth": 8,
            },
        },
    ]

    for binding in snapshot["bindings"]:
        provider = binding["providerId"]
        if provider == "huggingface":
            operation_ids = [
                "huggingface.file.download",
                "huggingface.models.list",
                "huggingface.repo.get",
            ]
        else:
            operation_ids = (
                sorted(preflight.CONNECTOR_MODEL_READONLY_OPERATIONS[provider])
                if include_live_catalog
                else sorted(
                    operation
                    for operation in preflight.CONNECTOR_OPERATION_POLICIES
                    if operation.startswith("cloud.") and operation != "cloud.resource.restart"
                )
            )
        binding["tools"] = []
        catalog_item = next(
            item
            for item in snapshot["catalog"]["items"]
            if item["bindingId"] == binding["bindingId"]
        )
        catalog_item["tools"] = []
        for operation in operation_ids:
            tool_id, resource_projection, mutating = preflight.CONNECTOR_OPERATION_POLICIES[
                operation
            ]
            binding_tool = {
                "toolId": tool_id,
                "operationId": operation,
                "resourceProjection": resource_projection,
                "mutating": mutating,
            }
            binding["tools"].append(binding_tool)
            if include_live_catalog:
                catalog_item["tools"].append(
                    {
                        "id": tool_id,
                        "name": f"Read {operation}",
                        "description": "Read one issued provider scope.",
                        "readOnly": not mutating,
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    }
                )
        if provider in {"azure", "gcp"}:
            profile = next(row for row in snapshot["profiles"] if row["providerId"] == provider)
            connection = next(
                row for row in snapshot["connections"] if row["providerId"] == provider
            )
            profile["readOnly"] = include_live_catalog
            connection["readOnly"] = include_live_catalog

    base_bindings_path = Path(compose["configs"]["connector-bindings"]["file"])
    base_catalog_path = Path(compose["configs"]["connector-tool-catalog"]["file"])
    base_bindings_path.write_text(
        json.dumps({"schemaVersion": 1, "bindings": base_bindings}), encoding="utf-8"
    )
    base_catalog_path.write_text(
        json.dumps({"schemaVersion": 1, "items": base_catalog}), encoding="utf-8"
    )
    compose["services"]["computer-runtime"] = {}
    snapshot["profiles"].extend(base_profiles)
    snapshot["connections"].extend(base_connections)
    snapshot["bindings"] = base_bindings + snapshot["bindings"]
    snapshot["catalog"] = {
        "schemaVersion": 1,
        "items": (base_catalog + snapshot["catalog"]["items"] if include_live_catalog else []),
    }
    return snapshot, compose


def _github_image_readiness_fixture(
    tmp_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a full issued snapshot whose GitHub scope matches its frozen bootstrap config."""

    snapshot, compose = _listener_repair_readiness_fixture(tmp_path, include_live_catalog=True)
    bindings_path = Path(compose["configs"]["connector-bindings"]["file"])
    catalog_path = Path(compose["configs"]["connector-tool-catalog"]["file"])
    bindings_path.write_text(
        json.dumps({"schemaVersion": 1, "bindings": snapshot["bindings"]}), encoding="utf-8"
    )
    catalog_path.write_text(
        json.dumps(snapshot["catalog"]),
        encoding="utf-8",
    )
    bootstrap_path = Path(compose["configs"]["connector-bootstrap"]["file"])
    bootstrap = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    github_profile = next(row for row in snapshot["profiles"] if row["providerId"] == "github")
    bootstrap["github"] = {
        "expectedAccessMode": "workspace_operator",
        "repositories": github_profile["targetScope"]["repositories"],
    }
    bootstrap_path.write_text(json.dumps(bootstrap), encoding="utf-8")
    return snapshot, compose


def test_github_image_readiness_returns_only_exact_issued_binding_refs(
    tmp_path: Path,
) -> None:
    """Join all ten GitHub connection refs and scopes to the frozen issued projection."""

    snapshot, compose = _github_image_readiness_fixture(tmp_path)
    receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
        phase="issued-connector-activation",
        image_update_target="github-issues",
    )

    expected_refs = sorted(
        row["connectionRef"] for row in snapshot["connections"] if row["providerId"] == "github"
    )
    assert receipt["status"] == "LIVE_VERIFIED"
    assert receipt["bindingCount"] == 14
    assert receipt["connectionCount"] == 14
    assert receipt["githubConnectionRefs"] == expected_refs

    changed_scope = copy.deepcopy(snapshot)
    github_profile = next(row for row in changed_scope["profiles"] if row["providerId"] == "github")
    github_profile["targetScope"]["repositories"][0]["name"] = "outside-frozen-scope"
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="live GitHub profile repository scope differs from the frozen bootstrap config",
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=changed_scope,
            compose=compose,
            phase="issued-connector-activation",
            image_update_target="github-issues",
        )

    missing_connection = copy.deepcopy(snapshot)
    missing_connection["connections"] = [
        row for row in missing_connection["connections"] if row["providerId"] != "github"
    ] + [row for row in snapshot["connections"] if row["providerId"] == "github"][:-1]
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="one live profile and exactly ten issued connections/bindings",
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=missing_connection,
            compose=compose,
            phase="issued-connector-activation",
            image_update_target="github-issues",
        )


def test_listener_repair_precheck_verifies_issued_state_without_live_catalog(
    tmp_path: Path,
) -> None:
    """Verify actual 14 issued rows and scopes while explicitly leaving ListTools NOT_RUN.

    中文：修复前证明 owner、连接、scope 与只读 operation map，但不声称目录已读取。
    """

    snapshot, compose = _listener_repair_readiness_fixture(tmp_path, include_live_catalog=False)
    receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
        phase="provider-bootstrap",
        listener_repair=True,
        include_live_catalog=False,
    )

    assert receipt["status"] == "ISSUED_STATE_VERIFIED"
    assert receipt["catalogStatus"] == "NOT_LIVE_CATALOG"
    assert receipt["catalogSha256"] is None
    assert receipt["profileCount"] == 5
    assert receipt["connectionCount"] == 14
    assert receipt["bindingCount"] == 14
    assert receipt["verifiedProviderIds"] == [
        "azure",
        "computer",
        "gcp",
        "github",
        "huggingface",
    ]
    assert len(receipt["issuedStateSha256"]) == 64


@pytest.mark.parametrize("provider", ["azure", "gcp"])
def test_listener_repair_precheck_requires_complete_cloud_issued_operation_map(
    tmp_path: Path,
    provider: str,
) -> None:
    """Metadata-only Exchange export must include the full 13-operation Cloud map."""

    snapshot, compose = _listener_repair_readiness_fixture(tmp_path, include_live_catalog=False)
    binding = next(row for row in snapshot["bindings"] if row["providerId"] == provider)
    full_cloud_operations = {
        operation
        for operation in preflight.CONNECTOR_OPERATION_POLICIES
        if operation.startswith("cloud.") and operation != "cloud.resource.restart"
    }
    assert len(full_cloud_operations) == 13
    assert {tool["operationId"] for tool in binding["tools"]} == full_cloud_operations

    binding["tools"].pop()
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="complete canonical operation map",
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=snapshot,
            compose=compose,
            phase="provider-bootstrap",
            listener_repair=True,
            include_live_catalog=False,
        )

    unsafe_hf_binding = copy.deepcopy(snapshot)
    hf_binding = next(
        row for row in unsafe_hf_binding["bindings"] if row["providerId"] == "huggingface"
    )
    hf_binding["tools"].append(
        {
            "toolId": "huggingface.file.upload",
            "operationId": "huggingface.file.upload",
            "resourceProjection": "huggingface_repository",
            "mutating": True,
        }
    )
    with pytest.raises(preflight.PreflightError, match="write, CLI, duplicate"):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=unsafe_hf_binding,
            compose=compose,
            phase="provider-bootstrap",
            listener_repair=True,
            include_live_catalog=False,
        )


def test_listener_repair_issued_digest_survives_expected_live_catalog_projection(
    tmp_path: Path,
) -> None:
    """Normalize only catalog-derived fields while retaining issued profile and scope identity."""

    before_path = tmp_path / "before"
    after_path = tmp_path / "after"
    before_path.mkdir()
    after_path.mkdir()
    before, _before_compose = _listener_repair_readiness_fixture(
        before_path, include_live_catalog=False
    )
    after, _after_compose = _listener_repair_readiness_fixture(
        after_path, include_live_catalog=True
    )

    assert stage_transition._exchange_issued_metadata_sha256(
        before
    ) == stage_transition._exchange_issued_metadata_sha256(after)

    changed_scope = copy.deepcopy(after)
    next(row for row in changed_scope["connections"] if row["providerId"] == "azure")[
        "targetScope"
    ]["targetRef"] = "different-issued-target"
    assert stage_transition._exchange_issued_metadata_sha256(
        before
    ) != stage_transition._exchange_issued_metadata_sha256(changed_scope)


def test_listener_repair_postcheck_requires_full_live_catalog_for_all_14_rows(
    tmp_path: Path,
) -> None:
    """Require the native post-repair catalog while preserving frozen Navigator's 11 rows."""

    snapshot, compose = _listener_repair_readiness_fixture(tmp_path, include_live_catalog=True)
    receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
        phase="provider-bootstrap",
        listener_repair=True,
        include_live_catalog=True,
    )

    assert receipt["status"] == "LIVE_VERIFIED"
    assert receipt["catalogStatus"] == "LIVE_VERIFIED"
    assert receipt["connectionCount"] == 14
    assert receipt["bindingCount"] == 14
    assert receipt["verifiedProviderIds"] == [
        "azure",
        "computer",
        "gcp",
        "github",
        "huggingface",
    ]
    assert receipt["issuedStateSha256"] == stage_transition._exchange_issued_metadata_sha256(
        snapshot
    )

    changed_scope = copy.deepcopy(snapshot)
    next(row for row in changed_scope["connections"] if row["providerId"] == "azure")[
        "targetScope"
    ]["targetRef"] = "unissued-scope"
    with pytest.raises(
        stage_transition.StageTransitionError, match="cloud profile scope/read-only"
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=changed_scope,
            compose=compose,
            phase="provider-bootstrap",
            listener_repair=True,
            include_live_catalog=True,
        )

    missing_catalog = copy.deepcopy(snapshot)
    missing_catalog["catalog"]["items"] = [
        item for item in missing_catalog["catalog"]["items"] if item["providerId"] != "huggingface"
    ]
    with pytest.raises(
        preflight.PreflightError, match="must match every non-empty connector binding"
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=missing_catalog,
            compose=compose,
            phase="provider-bootstrap",
            listener_repair=True,
            include_live_catalog=True,
        )


def test_listener_repair_hf_list_operation_matches_issued_repo_type(tmp_path: Path) -> None:
    """Do not let a model-scoped HF profile expose dataset/space list operations."""

    snapshot, compose = _listener_repair_readiness_fixture(tmp_path, include_live_catalog=True)
    hf_binding = next(row for row in snapshot["bindings"] if row["providerId"] == "huggingface")
    operations = {tool["operationId"] for tool in hf_binding["tools"]}
    assert operations == {
        "huggingface.file.download",
        "huggingface.models.list",
        "huggingface.repo.get",
    }

    next(tool for tool in hf_binding["tools"] if tool["operationId"] == "huggingface.models.list")[
        "operationId"
    ] = "huggingface.datasets.list"
    next(tool for tool in hf_binding["tools"] if tool["toolId"] == "huggingface.models.list")[
        "toolId"
    ] = "huggingface.datasets.list"
    hf_catalog = next(
        item for item in snapshot["catalog"]["items"] if item["providerId"] == "huggingface"
    )
    next(tool for tool in hf_catalog["tools"] if tool["id"] == "huggingface.models.list")["id"] = (
        "huggingface.datasets.list"
    )
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="list operation does not match the issued repository type",
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=snapshot,
            compose=compose,
            phase="provider-bootstrap",
            listener_repair=True,
            include_live_catalog=True,
        )


def test_listener_repair_precheck_rejects_new_or_unconfigured_issued_connections(
    tmp_path: Path,
) -> None:
    """Do not infer or accept an extra provider binding beyond the exact current 14 rows."""

    snapshot, compose = _listener_repair_readiness_fixture(tmp_path, include_live_catalog=False)
    extra = copy.deepcopy(snapshot["bindings"][-1])
    extra["connectionRef"] = "90000000-0000-4000-8000-000000000001"
    extra["bindingId"] = f"exchange-runpod-{extra['connectionRef']}"
    extra["providerId"] = "runpod"
    snapshot["bindings"].append(extra)
    with pytest.raises(stage_transition.StageTransitionError, match="exactly the existing 14"):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=snapshot,
            compose=compose,
            phase="provider-bootstrap",
            listener_repair=True,
            include_live_catalog=False,
        )


def test_listener_repair_reads_existing_exchange_store_without_provider_list_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use Exchange's canonical metadata exporter, with no live catalog request before repair."""

    snapshot, compose = _listener_repair_readiness_fixture(tmp_path, include_live_catalog=False)
    captured: list[str] = []

    def checked(argv: list[str], **_kwargs: Any) -> str:
        captured.extend(argv)
        return json.dumps(snapshot, separators=(",", ":"))

    monkeypatch.setattr(runtime_update, "_checked_stdout", checked)
    receipt = stage_transition._read_live_exchange_issued_state(
        compose=compose,
        container_id="a" * 64,
        image_id=_image_id(78),
        docker_prefix=["docker"],
    )

    assert captured[:4] == [
        "docker",
        "exec",
        "a" * 64,
        stage_transition.EXCHANGE_SECRET_ENV_ENTRYPOINT,
    ]
    assert captured[captured.index("-c") + 1] == stage_transition._EXCHANGE_ISSUED_STATE_PYTHON
    assert "include_profile_metadata=True" in stage_transition._EXCHANGE_ISSUED_STATE_PYTHON
    assert "include_live_catalog=False" in stage_transition._EXCHANGE_ISSUED_STATE_PYTHON
    assert "list_tool_catalog" not in stage_transition._EXCHANGE_ISSUED_STATE_PYTHON
    assert receipt["catalogStatus"] == "NOT_LIVE_CATALOG"
    assert receipt["exchangeContainerId"] == "a" * 64
    assert receipt["exchangeImageId"] == _image_id(78)


@pytest.mark.parametrize("target", ["exchange", "cloud-management-phase2"])
def test_bootstrap_image_readiness_preserves_only_existing_bindings(
    tmp_path: Path,
    target: str,
) -> None:
    """A bootstrap-phase image update verifies the current catalog without requiring future providers.

    中文：Bootstrap 镜像更新只校验当前已启用绑定，不要求尚未签发的连接器。
    """

    snapshot, compose = _exchange_readiness_fixture(tmp_path)
    bindings: list[dict[str, Any]] = []
    catalog_items: list[dict[str, Any]] = []
    existing = [
        ("github", f"github-connection-{index}", "github-profile", index) for index in range(10)
    ]
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
    github_repositories = [
        {
            "repositoryId": f"github-repository-{index}",
            "owner": "example-org",
            "name": f"repository-{index}",
        }
        for index in range(10)
    ]
    snapshot["profiles"] = [
        {
            "profileRef": "github-profile",
            "profileAlias": None,
            "providerId": "github",
            "authStatus": "healthy",
            "accessMode": "workspace_operator",
            "authMode": "github_app",
            "targetScope": {"repositories": github_repositories},
        },
        {
            "profileRef": "computer-profile",
            "profileAlias": "computer-profile",
            "providerId": "computer",
            "accessMode": "workspace_operator",
            "targetScope": {
                "maxCommandTimeoutMs": 30000,
                "maxFileBytes": 1048576,
                "maxReadBytes": 1048576,
                "maxDirectoryDepth": 8,
            },
        },
    ]
    snapshot["connections"] = [
        {
            "connectionRef": f"github-connection-{index}",
            "providerId": "github",
            "profileRef": "github-profile",
            "profileAlias": None,
            "targetAlias": None,
            "accessMode": "workspace_operator",
            "readOnly": False,
            "targetScope": {
                "repositoryId": f"github-repository-{index}",
                "repository": f"example-org/repository-{index}",
            },
        }
        for index in range(10)
    ] + [
        {
            "connectionRef": "computer-connection",
            "providerId": "computer",
            "profileRef": "computer-profile",
            "profileAlias": "computer-profile",
            "accessMode": "workspace_operator",
            "readOnly": False,
        },
    ]
    snapshot["bindings"] = bindings
    snapshot["catalog"] = catalog_document

    receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
        phase="provider-bootstrap",
        image_update_target=target,
    )

    assert receipt["verifiedProviderIds"] == ["computer", "github"]
    assert receipt["bindingCount"] == 11
    assert receipt["profileCount"] == 2
    assert receipt["connectionCount"] == 11
    assert receipt["profileRefByProvider"] == {}

    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["bindings"][0]["tools"][0]["resourceProjection"] = "other_scope"
    with pytest.raises(stage_transition.StageTransitionError, match="issued bindings differ"):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=changed_snapshot,
            compose=compose,
            phase="provider-bootstrap",
            image_update_target=target,
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
            image_update_target=target,
        )


@pytest.mark.parametrize("unexpected_field", ["credentialSecretRef", "accessToken"])
def test_exchange_readiness_safe_shape_matches_the_official_issued_fourteen_dto(
    unexpected_field: str,
) -> None:
    """Accept the exact 10 GitHub + HF/Azure/GCP + computer DTO and reject secret extras."""

    github_repositories = [
        {
            "repositoryId": f"github-repository-{index}",
            "owner": "example-org",
            "name": f"repository-{index}",
        }
        for index in range(10)
    ]
    hf_repository = {
        "repoType": "model",
        "repoId": "example-org/private-model",
        "permissions": ["read"],
    }
    profiles = [
        {
            "profileRef": "github-profile",
            "profileAlias": None,
            "providerId": "github",
            "authStatus": "healthy",
            "authMode": "github_app",
            "accessMode": "workspace_operator",
            "targetScope": {"repositories": github_repositories},
        },
        {
            "profileRef": "hf-profile",
            "profileAlias": "hf-primary",
            "providerId": "huggingface",
            "authStatus": "healthy",
            "authMode": "device_oauth",
            "accessMode": "workspace_operator",
            "targetScope": {"repositories": [hf_repository]},
        },
        {
            "profileRef": "azure-profile",
            "profileAlias": "azure-primary",
            "providerId": "azure",
            "accessMode": "workspace_operator",
            "targetScope": {
                "targets": {"azure-read": {"targetRef": "sub-123"}},
            },
            "readOnly": True,
        },
        {
            "profileRef": "gcp-profile",
            "profileAlias": "gcp-primary",
            "providerId": "gcp",
            "accessMode": "workspace_operator",
            "targetScope": {
                "targets": {"gcp-read": {"targetRef": "project-123"}},
            },
            "readOnly": True,
        },
        {
            "profileRef": "computer-profile",
            "profileAlias": "computer-profile",
            "providerId": "computer",
            "accessMode": "workspace_operator",
            "targetScope": {
                "maxCommandTimeoutMs": 30000,
                "maxFileBytes": 1048576,
                "maxReadBytes": 1048576,
                "maxDirectoryDepth": 8,
            },
        },
    ]
    connections = [
        {
            "connectionRef": f"github-connection-{index}",
            "providerId": "github",
            "profileRef": "github-profile",
            "profileAlias": None,
            "targetAlias": None,
            "accessMode": "workspace_operator",
            "readOnly": False,
            "targetScope": {
                "repositoryId": f"github-repository-{index}",
                "repository": f"example-org/repository-{index}",
            },
        }
        for index in range(10)
    ]
    connections.extend(
        [
            {
                "connectionRef": "hf-connection",
                "providerId": "huggingface",
                "profileRef": "hf-profile",
                "profileAlias": "hf-primary",
                "targetAlias": "hf-model",
                "accessMode": "workspace_operator",
                "readOnly": True,
                "targetScope": hf_repository,
            },
            {
                "connectionRef": "azure-connection",
                "providerId": "azure",
                "profileRef": "azure-profile",
                "profileAlias": "azure-primary",
                "targetAlias": "azure-read",
                "accessMode": "workspace_operator",
                "readOnly": True,
                "targetScope": {"targetAlias": "azure-read", "targetRef": "sub-123"},
            },
            {
                "connectionRef": "gcp-connection",
                "providerId": "gcp",
                "profileRef": "gcp-profile",
                "profileAlias": "gcp-primary",
                "targetAlias": "gcp-read",
                "accessMode": "workspace_operator",
                "readOnly": True,
                "targetScope": {"targetAlias": "gcp-read", "targetRef": "project-123"},
            },
            {
                "connectionRef": "computer-connection",
                "providerId": "computer",
                "profileRef": "computer-profile",
                "profileAlias": "computer-profile",
                "accessMode": "workspace_operator",
                "readOnly": False,
            },
        ]
    )
    assert len(connections) == 14
    snapshot = {"profiles": profiles, "connections": connections}

    stage_transition._assert_exchange_readiness_safe_shape(snapshot)

    missing_github_alias = copy.deepcopy(snapshot)
    del missing_github_alias["connections"][0]["targetAlias"]
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="Exchange readiness connection contains unsupported fields",
    ):
        stage_transition._assert_exchange_readiness_safe_shape(missing_github_alias)

    computer_with_scope = copy.deepcopy(snapshot)
    computer_with_scope["connections"][-1]["targetScope"] = {"targets": {}}
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="Exchange readiness connection contains unsupported fields",
    ):
        stage_transition._assert_exchange_readiness_safe_shape(computer_with_scope)

    unsafe = copy.deepcopy(snapshot)
    unsafe["connections"][-1][unexpected_field] = "secret://must-not-be-exported"
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="Exchange readiness connection contains unsupported fields",
    ):
        stage_transition._assert_exchange_readiness_safe_shape(unsafe)


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

    cloud_image_receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
        phase="issued-connector-activation",
        image_update_target="cloud-management-phase2",
    )
    assert cloud_image_receipt["status"] == "LIVE_VERIFIED"
    assert cloud_image_receipt["verifiedProviderIds"] == ["azure", "gcp", "huggingface"]
    assert cloud_image_receipt["connectionRefByProvider"] == {
        row["providerId"]: row["connectionRef"]
        for row in snapshot["connections"]
        if row["providerId"] in {"azure", "gcp"}
    }

    hf_connection_ref = next(
        row["connectionRef"]
        for row in snapshot["connections"]
        if row["providerId"] == "huggingface"
    )
    hf_image_receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
        phase="issued-connector-activation",
        image_update_target="huggingface",
    )
    assert hf_image_receipt["huggingfaceConnectionRef"] == hf_connection_ref

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


def test_huggingface_readiness_checkpoint_uses_live_connection_reference() -> None:
    """Bind the HF restart window to the exact connection joined by Exchange readiness."""

    connection_ref = "e5e1464b-00ec-4c85-9e71-1d52eab44a38"
    plan = {
        "imageUpdateTarget": "huggingface",
        "activeServices": {"exchange": {"containerId": "a" * 64, "imageId": _image_id(44)}},
        "candidatePreflight": {
            "expectedProviderScopeDigests": {
                "azure": "b" * 64,
                "gcp": "c" * 64,
                "huggingface": "d" * 64,
            }
        },
    }
    journal = {
        "exchangeReadiness": {
            "receipt": {
                "status": "LIVE_VERIFIED",
                "catalogStatus": "LIVE_VERIFIED",
                "exchangeContainerId": "a" * 64,
                "exchangeImageId": _image_id(44),
                "bindingCount": 14,
                "connectionCount": 14,
                "verifiedProviderIds": ["azure", "gcp", "huggingface"],
                "snapshotSha256": "1" * 64,
                "huggingfaceConnectionRef": connection_ref,
            }
        }
    }

    plan["phase"] = "issued-connector-activation"
    receipt = stage_transition._image_update_exchange_readiness_checkpoint(plan, journal)
    assert receipt["verified"] is True
    assert receipt["bindingCount"] == 14
    assert (
        receipt["connectionRefsSha256"]
        == hashlib.sha256(
            json.dumps(
                {"huggingface": hashlib.sha256(connection_ref.encode("utf-8")).hexdigest()},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )

    journal["exchangeReadiness"]["receipt"]["huggingfaceConnectionRef"] = "not-a-uuid"
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="lacks the exact selected huggingface connection",
    ):
        stage_transition._image_update_exchange_readiness_checkpoint(plan, journal)


def test_cloud_readiness_checkpoint_uses_exact_issued_provider_connections() -> None:
    """Bind Cloud restart checks to the live Azure, GCP, and configured RunPod refs."""

    provider_refs = {
        "azure": "e5e1464b-00ec-4c85-9e71-1d52eab44a38",
        "gcp": "8c473baa-3d22-4ab4-9d0e-f1086b54a400",
        "runpod": "d0b0b832-a81c-4c39-94c9-d92405f2e6c2",
    }
    plan = {
        "imageUpdateTarget": "cloud-management-phase2",
        "phase": "issued-connector-activation",
        "activeServices": {"exchange": {"containerId": "a" * 64, "imageId": _image_id(44)}},
        "candidatePreflight": {
            "expectedProviderScopeDigests": {
                "azure": "b" * 64,
                "gcp": "c" * 64,
                "runpod": "d" * 64,
                "huggingface": "e" * 64,
            }
        },
    }
    journal = {
        "exchangeReadiness": {
            "receipt": {
                "status": "LIVE_VERIFIED",
                "catalogStatus": "LIVE_VERIFIED",
                "exchangeContainerId": "a" * 64,
                "exchangeImageId": _image_id(44),
                "bindingCount": 14,
                "connectionCount": 14,
                "verifiedProviderIds": ["azure", "gcp", "huggingface", "runpod"],
                "snapshotSha256": "1" * 64,
                "connectionRefByProvider": provider_refs,
            }
        }
    }

    receipt = stage_transition._image_update_exchange_readiness_checkpoint(plan, journal)
    expected_digest = hashlib.sha256(
        json.dumps(
            {
                provider: hashlib.sha256(connection_ref.encode("utf-8")).hexdigest()
                for provider, connection_ref in sorted(provider_refs.items())
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert receipt["verified"] is True
    assert receipt["connectionRefsSha256"] == expected_digest

    journal["exchangeReadiness"]["receipt"]["connectionRefByProvider"].pop("runpod")
    with pytest.raises(
        stage_transition.StageTransitionError, match="exact selected provider connections"
    ):
        stage_transition._image_update_exchange_readiness_checkpoint(plan, journal)

    del plan["candidatePreflight"]["expectedProviderScopeDigests"]["runpod"]
    plan["candidatePreflight"]["expectedProviderScopeDigests"]["huggingface"] = "e" * 64
    journal["exchangeReadiness"]["receipt"]["connectionRefByProvider"] = {
        "azure": provider_refs["azure"],
        "gcp": provider_refs["gcp"],
    }
    journal["exchangeReadiness"]["receipt"]["verifiedProviderIds"] = [
        "azure",
        "gcp",
        "huggingface",
    ]
    receipt = stage_transition._image_update_exchange_readiness_checkpoint(plan, journal)
    assert receipt["verified"] is True


def test_github_readiness_checkpoint_hashes_all_ten_exact_connection_refs() -> None:
    """Require every GitHub connection from the issued 14-binding snapshot before mutation."""

    connection_refs = [f"10000000-0000-4000-8000-{index:012d}" for index in range(1, 11)]
    plan = {
        "imageUpdateTarget": "github-issues",
        "phase": "issued-connector-activation",
        "activeServices": {"exchange": {"containerId": "a" * 64, "imageId": _image_id(44)}},
        "candidatePreflight": {
            "expectedProviderScopeDigests": {
                "azure": "b" * 64,
                "gcp": "c" * 64,
                "huggingface": "d" * 64,
            }
        },
    }
    journal = {
        "exchangeReadiness": {
            "receipt": {
                "status": "LIVE_VERIFIED",
                "catalogStatus": "LIVE_VERIFIED",
                "exchangeContainerId": "a" * 64,
                "exchangeImageId": _image_id(44),
                "bindingCount": 14,
                "connectionCount": 14,
                "verifiedProviderIds": ["azure", "gcp", "huggingface"],
                "snapshotSha256": "1" * 64,
                "githubConnectionRefs": connection_refs,
            }
        }
    }

    checkpoint = stage_transition._image_update_exchange_readiness_checkpoint(plan, journal)
    expected_digest = hashlib.sha256(
        json.dumps(
            {
                "github": [
                    hashlib.sha256(ref.encode("utf-8")).hexdigest()
                    for ref in sorted(connection_refs)
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert checkpoint["verified"] is True
    assert checkpoint["connectionRefsSha256"] == expected_digest

    journal["exchangeReadiness"]["receipt"]["githubConnectionRefs"] = connection_refs[:-1]
    with pytest.raises(stage_transition.StageTransitionError, match="exact selected github"):
        stage_transition._image_update_exchange_readiness_checkpoint(plan, journal)


def test_cloud_image_update_journal_keeps_bootstrap_and_issued_gates_separate(
    tmp_path: Path,
) -> None:
    """Keep the legacy runner-off gate while selecting monitor quiescence for issued updates."""

    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    target = "cloud-management-phase2"
    plan: dict[str, Any] = {
        "operationId": "00000000-0000-4000-8000-000000000001",
        "planSha256": "a" * 64,
        "project": "cyrene-teams-cloud",
        "targets": [target],
        "activeServices": {target: {"containerId": "b" * 64}},
        "candidateServices": {
            target: {
                "active": {"imageId": _image_id(10)},
                "candidate": {"imageId": _image_id(11)},
                "added": False,
            }
        },
        "edgeDelivery": {"containerId": "c" * 64, "revision": 0, "deadCount": 0},
        "navigatorDrain": {
            "containerId": "d" * 64,
            "instanceId": "11111111-1111-4111-8111-111111111111",
            "generation": 0,
        },
        "database": {"backupPath": "/private/database.snapshot"},
        "exchangeReadiness": {"required": True},
        "candidateSelectionFile": "/private/runtime-selection.json",
        "selectionBackupPath": "/private/previous-selection.json",
        "activeSelection": {"sha256": None},
    }

    issued_plan = {**plan, "phase": "issued-connector-activation", "imageUpdateTarget": target}
    issued_journal = stage_transition._make_journal(issued_plan, plan_path)
    assert issued_journal["cloudImageUpdate"]["required"] is True
    assert issued_journal["cloudRunnerIdle"]["required"] is False

    bootstrap_plan = {
        **plan,
        "phase": "provider-bootstrap",
        "imageUpdateTarget": target,
        "cloudRunnerState": {"liveContainerValue": "false"},
    }
    bootstrap_journal = stage_transition._make_journal(bootstrap_plan, plan_path)
    assert bootstrap_journal["cloudImageUpdate"]["required"] is False
    assert bootstrap_journal["cloudRunnerIdle"]["required"] is True
    assert bootstrap_journal["cloudRunnerIdle"]["planState"] == bootstrap_plan["cloudRunnerState"]


def test_github_image_update_journal_and_status_report_readiness_and_monitor_gates(
    tmp_path: Path,
) -> None:
    """Record both GitHub readiness checkpoints and all-binding monitor quiescence."""

    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    target = "github-issues"
    plan: dict[str, Any] = {
        "operationId": "00000000-0000-4000-8000-000000000001",
        "planSha256": "a" * 64,
        "project": "cyrene-teams-cloud",
        "phase": "issued-connector-activation",
        "imageUpdateTarget": target,
        "targets": [target],
        "activeServices": {target: {"containerId": "b" * 64}},
        "candidateServices": {
            target: {
                "active": {"imageId": _image_id(10)},
                "candidate": {"imageId": _image_id(11)},
                "added": False,
            }
        },
        "edgeDelivery": {"containerId": "c" * 64, "revision": 0, "deadCount": 0},
        "navigatorDrain": {
            "containerId": "d" * 64,
            "instanceId": "11111111-1111-4111-8111-111111111111",
            "generation": 0,
        },
        "database": {"backupPath": "/private/database.snapshot"},
        "exchangeReadiness": {"required": True},
        "candidateSelectionFile": "/private/runtime-selection.json",
        "selectionBackupPath": "/private/previous-selection.json",
        "activeSelection": {"sha256": None},
    }

    journal = stage_transition._make_journal(plan, plan_path)
    assert journal["githubIssuesImageUpdate"]["required"] is True
    assert journal["cloudImageUpdate"]["required"] is False
    journal["state"] = "APPLIED"
    journal["githubIssuesImageUpdate"].update(
        {
            "beforeReadiness": {"verified": True},
            "monitorQuiescence": {"verified": True},
            "afterReadiness": {"verified": True},
        }
    )

    summary = stage_transition._journal_summary(journal)
    assert summary["githubIssuesImageUpdateRequired"] is True
    assert summary["githubIssuesImageUpdateStatus"] == "VERIFIED"
    assert summary["githubIssuesMonitorQuiescenceStatus"] == "VERIFIED"
    assert summary["githubIssuesBeforeReadinessStatus"] == "LIVE_VERIFIED"
    assert summary["githubIssuesAfterReadinessStatus"] == "LIVE_VERIFIED"


def _create_huggingface_monitor_database(
    path: Path,
    *,
    connection_ref: str,
    actor: dict[str, str],
    next_check_seconds: int,
    active_monitor_lease: bool,
    in_flight_connector_calls: int,
) -> None:
    """Build the Exchange monitor tables needed by the read-only restart-window probe."""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE integration_connections (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            actor_issuer TEXT NOT NULL, actor_subject TEXT NOT NULL, enabled INTEGER NOT NULL,
            document TEXT NOT NULL
        );
        CREATE TABLE authorization_monitor_state (
            connection_id TEXT PRIMARY KEY, next_check_at TEXT NOT NULL, last_checked_at TEXT,
            claim_owner TEXT, claim_fence INTEGER NOT NULL DEFAULT 0, lease_expires_at TEXT
        );
        CREATE TABLE connector_invocations (
            organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, actor_issuer TEXT NOT NULL,
            actor_subject TEXT NOT NULL, request_id TEXT NOT NULL, connection_ref TEXT NOT NULL,
            provider_id TEXT NOT NULL, profile_ref TEXT NOT NULL, operation_id TEXT NOT NULL,
            resource_ref TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
            envelope_sha256 TEXT NOT NULL, state TEXT NOT NULL, operation_ref TEXT,
            grant_ref TEXT NOT NULL DEFAULT '', is_mutating INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, response_json TEXT,
            response_status INTEGER NOT NULL DEFAULT 200
        );
        """
    )
    connection_document = {
        "id": connection_ref,
        "organizationId": actor["organizationId"],
        "workspaceId": actor["workspaceId"],
        "actorIssuer": actor["issuer"],
        "actorSubject": actor["subject"],
        "provider": "hf",
        "enabled": True,
    }
    connection.execute(
        "INSERT INTO integration_connections VALUES(?,?,?,?,?,?,?)",
        (
            connection_ref,
            actor["organizationId"],
            actor["workspaceId"],
            actor["issuer"],
            actor["subject"],
            1,
            json.dumps(connection_document),
        ),
    )
    now = datetime.now(UTC)
    next_check_at = (now + timedelta(seconds=next_check_seconds)).isoformat()
    claim_owner = "authorization-monitor-test" if active_monitor_lease else None
    lease_expires_at = (now + timedelta(seconds=30)).isoformat() if active_monitor_lease else None
    connection.execute(
        "INSERT INTO authorization_monitor_state VALUES(?,?,?,?,?,?)",
        (
            connection_ref,
            next_check_at,
            None,
            claim_owner,
            1 if active_monitor_lease else 0,
            lease_expires_at,
        ),
    )
    for index in range(in_flight_connector_calls):
        connection.execute(
            "INSERT INTO connector_invocations "
            "(organization_id,workspace_id,actor_issuer,actor_subject,request_id,connection_ref,"
            "provider_id,profile_ref,operation_id,resource_ref,arguments_sha256,envelope_sha256,"
            "state,grant_ref,is_mutating,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                actor["organizationId"],
                actor["workspaceId"],
                actor["issuer"],
                actor["subject"],
                f"hf-call-{index}",
                connection_ref,
                "huggingface",
                "hf-primary",
                "huggingface.repo.get",
                "Qwen/Qwen2.5-0.5B-Instruct",
                "a" * 64,
                "b" * 64,
                "IN_PROGRESS",
                "sha256:" + "c" * 64,
                0,
                int(now.timestamp()),
                0,
            ),
        )
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    ("next_check_seconds", "active_monitor_lease", "in_flight_connector_calls", "verified"),
    [
        (900, False, 0, True),
        (900, True, 0, False),
        (900, False, 1, False),
        (300, False, 0, False),
    ],
)
def test_huggingface_monitor_guard_requires_idle_exact_connection_and_restart_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    next_check_seconds: int,
    active_monitor_lease: bool,
    in_flight_connector_calls: int,
    verified: bool,
) -> None:
    """Run the exact read-only Exchange SQL against a fixture and fail closed on each blocker."""

    connection_ref = "e5e1464b-00ec-4c85-9e71-1d52eab44a38"
    actor = {
        "issuer": "https://login.microsoftonline.com/tenant/v2.0",
        "subject": "actor-subject",
        "organizationId": "organization-id",
        "workspaceId": "workspace-id",
    }
    database_path = tmp_path / "integrations.sqlite3"
    _create_huggingface_monitor_database(
        database_path,
        connection_ref=connection_ref,
        actor=actor,
        next_check_seconds=next_check_seconds,
        active_monitor_lease=active_monitor_lease,
        in_flight_connector_calls=in_flight_connector_calls,
    )
    compose = {"name": "cyrene-teams-cloud", "services": {"exchange": {}}}
    monkeypatch.setattr(
        stage_transition,
        "_candidate_config_json",
        lambda _compose, _name: {
            "bindings": [{"providerId": "huggingface", "connectionRef": connection_ref}]
        },
    )
    monkeypatch.setattr(
        stage_transition.preflight, "_connector_bootstrap_actor", lambda _compose: actor
    )
    phases: list[str] = []

    def run_readonly_exchange_script(argv: list[str], **_kwargs: Any) -> str:
        script_index = argv.index("-c")
        script = argv[script_index + 1]
        arguments = list(argv[script_index + 2 :])
        assert json.loads(arguments[1]) == [
            {"providerId": "huggingface", "connectionRef": connection_ref}
        ]
        arguments[0] = str(database_path)
        result = subprocess.run(
            [sys.executable, "-c", script, *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise runtime_update.RuntimeUpdateError("read-only Exchange probe failed")
        return result.stdout

    monkeypatch.setattr(runtime_update, "_checked_stdout", run_readonly_exchange_script)
    monkeypatch.setattr(
        stage_transition,
        "_write_journal",
        lambda _path, _journal, phase: phases.append(phase),
    )
    plan = {
        "imageUpdateTarget": "huggingface",
        "activeServices": {"exchange": {"containerId": "a" * 64}},
        "policy": {"healthTimeoutSeconds": 300},
    }
    journal = {
        "exchangeReadiness": {"receipt": {"huggingfaceConnectionRef": connection_ref}},
        "huggingfaceImageUpdate": {"required": True},
    }

    if verified:
        result = stage_transition._verify_provider_monitor_quiescence(
            plan=plan,
            candidate_compose=compose,
            journal=journal,
            journal_path=tmp_path / "journal.json",
            docker_prefix=["docker"],
        )
        assert result["verified"] is True
        assert result["requiredWindowSeconds"] == 360
        assert journal["huggingfaceImageUpdate"]["monitorQuiescence"] == result
        assert phases == ["CONNECTOR_MONITOR_QUIESCENCE_VERIFIED"]
    else:
        with pytest.raises(stage_transition.StageTransitionError, match="blocked because"):
            stage_transition._verify_provider_monitor_quiescence(
                plan=plan,
                candidate_compose=compose,
                journal=journal,
                journal_path=tmp_path / "journal.json",
                docker_prefix=["docker"],
            )
        assert journal["huggingfaceImageUpdate"]["monitorQuiescence"]["verified"] is False
        assert phases == ["CONNECTOR_MONITOR_NOT_QUIESCENT"]


def test_huggingface_monitor_guard_never_guesses_a_connection_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live receipt that does not join to the selected binding blocks before the DB read."""

    selected_connection = "e5e1464b-00ec-4c85-9e71-1d52eab44a38"
    different_binding_connection = "8c473baa-3d22-4ab4-9d0e-f1086b54a400"
    monkeypatch.setattr(
        stage_transition,
        "_candidate_config_json",
        lambda _compose, _name: {
            "bindings": [
                {
                    "providerId": "huggingface",
                    "connectionRef": different_binding_connection,
                }
            ]
        },
    )
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: pytest.fail(
            "monitor SQL must not run for an unjoined connection"
        ),
    )

    with pytest.raises(
        stage_transition.StageTransitionError,
        match="does not match its single frozen candidate binding",
    ):
        stage_transition._verify_provider_monitor_quiescence(
            plan={
                "imageUpdateTarget": "huggingface",
                "activeServices": {"exchange": {"containerId": "a" * 64}},
                "policy": {"healthTimeoutSeconds": 300},
            },
            candidate_compose={"services": {}},
            journal={
                "exchangeReadiness": {"receipt": {"huggingfaceConnectionRef": selected_connection}},
                "huggingfaceImageUpdate": {"required": True},
            },
            journal_path=Path("/private/journal.json"),
            docker_prefix=["docker"],
        )


def _create_cloud_monitor_database(
    path: Path,
    *,
    connection_refs: dict[str, str],
    actor: dict[str, str],
    next_check_seconds: dict[str, int],
    active_monitor_leases: set[str],
    in_flight_calls: dict[str, int],
    cloud_refresh_states: list[str],
) -> None:
    """Build the exact Exchange rows used by the read-only Cloud restart-window probe."""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE integration_connections (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            actor_issuer TEXT NOT NULL, actor_subject TEXT NOT NULL, enabled INTEGER NOT NULL,
            document TEXT NOT NULL
        );
        CREATE TABLE authorization_monitor_state (
            connection_id TEXT PRIMARY KEY, next_check_at TEXT NOT NULL, last_checked_at TEXT,
            claim_owner TEXT, claim_fence INTEGER NOT NULL DEFAULT 0, lease_expires_at TEXT
        );
        CREATE TABLE connector_invocations (
            organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, actor_issuer TEXT NOT NULL,
            actor_subject TEXT NOT NULL, request_id TEXT NOT NULL, connection_ref TEXT NOT NULL,
            provider_id TEXT NOT NULL, profile_ref TEXT NOT NULL, operation_id TEXT NOT NULL,
            resource_ref TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
            envelope_sha256 TEXT NOT NULL, state TEXT NOT NULL, operation_ref TEXT,
            grant_ref TEXT NOT NULL DEFAULT '', is_mutating INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, response_json TEXT,
            response_status INTEGER NOT NULL DEFAULT 200
        );
        CREATE TABLE cloud_refresh_outbox (state TEXT NOT NULL);
        """
    )
    now = datetime.now(UTC)
    for provider, connection_ref in sorted(connection_refs.items()):
        connection_document = {
            "id": connection_ref,
            "organizationId": actor["organizationId"],
            "workspaceId": actor["workspaceId"],
            "actorIssuer": actor["issuer"],
            "actorSubject": actor["subject"],
            "provider": provider,
            "enabled": True,
        }
        connection.execute(
            "INSERT INTO integration_connections VALUES(?,?,?,?,?,?,?)",
            (
                connection_ref,
                actor["organizationId"],
                actor["workspaceId"],
                actor["issuer"],
                actor["subject"],
                1,
                json.dumps(connection_document),
            ),
        )
        next_check_at = (now + timedelta(seconds=next_check_seconds[provider])).isoformat()
        has_lease = provider in active_monitor_leases
        claim_owner = f"{provider}-authorization-monitor" if has_lease else None
        lease_expires_at = (now + timedelta(seconds=30)).isoformat() if has_lease else None
        connection.execute(
            "INSERT INTO authorization_monitor_state VALUES(?,?,?,?,?,?)",
            (
                connection_ref,
                next_check_at,
                None,
                claim_owner,
                1 if has_lease else 0,
                lease_expires_at,
            ),
        )
        for index in range(in_flight_calls.get(provider, 0)):
            connection.execute(
                "INSERT INTO connector_invocations "
                "(organization_id,workspace_id,actor_issuer,actor_subject,request_id,connection_ref,"
                "provider_id,profile_ref,operation_id,resource_ref,arguments_sha256,envelope_sha256,"
                "state,grant_ref,is_mutating,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    actor["organizationId"],
                    actor["workspaceId"],
                    actor["issuer"],
                    actor["subject"],
                    f"{provider}-call-{index}",
                    connection_ref,
                    provider,
                    f"{provider}-primary",
                    "cloud.resources.list",
                    "configured-resource-scope",
                    "a" * 64,
                    "b" * 64,
                    "IN_PROGRESS",
                    "sha256:" + "c" * 64,
                    0,
                    int(now.timestamp()),
                    0,
                ),
            )
    connection.executemany(
        "INSERT INTO cloud_refresh_outbox(state) VALUES(?)",
        [(state,) for state in cloud_refresh_states],
    )
    connection.commit()
    connection.close()


def _create_github_monitor_database(
    path: Path,
    *,
    connection_refs: list[str],
    actor: dict[str, str],
    next_check_seconds: int,
    active_monitor_ref: str | None,
    in_flight_ref: str | None,
    missing_monitor_ref: str | None = None,
) -> None:
    """Build ten actor-scoped GitHub connections and their monitor/call rows."""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE integration_connections (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            actor_issuer TEXT NOT NULL, actor_subject TEXT NOT NULL, enabled INTEGER NOT NULL,
            document TEXT NOT NULL
        );
        CREATE TABLE authorization_monitor_state (
            connection_id TEXT PRIMARY KEY, next_check_at TEXT NOT NULL, last_checked_at TEXT,
            claim_owner TEXT, claim_fence INTEGER NOT NULL DEFAULT 0, lease_expires_at TEXT
        );
        CREATE TABLE connector_invocations (
            organization_id TEXT NOT NULL, workspace_id TEXT NOT NULL, actor_issuer TEXT NOT NULL,
            actor_subject TEXT NOT NULL, request_id TEXT NOT NULL, connection_ref TEXT NOT NULL,
            provider_id TEXT NOT NULL, profile_ref TEXT NOT NULL, operation_id TEXT NOT NULL,
            resource_ref TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
            envelope_sha256 TEXT NOT NULL, state TEXT NOT NULL, operation_ref TEXT,
            grant_ref TEXT NOT NULL DEFAULT '', is_mutating INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, response_json TEXT,
            response_status INTEGER NOT NULL DEFAULT 200
        );
        """
    )
    now = datetime.now(UTC)
    for index, connection_ref in enumerate(connection_refs):
        document = {
            "id": connection_ref,
            "organizationId": actor["organizationId"],
            "workspaceId": actor["workspaceId"],
            "actorIssuer": actor["issuer"],
            "actorSubject": actor["subject"],
            "provider": "github",
            "enabled": True,
        }
        connection.execute(
            "INSERT INTO integration_connections VALUES(?,?,?,?,?,?,?)",
            (
                connection_ref,
                actor["organizationId"],
                actor["workspaceId"],
                actor["issuer"],
                actor["subject"],
                1,
                json.dumps(document),
            ),
        )
        if connection_ref != missing_monitor_ref:
            has_lease = connection_ref == active_monitor_ref
            connection.execute(
                "INSERT INTO authorization_monitor_state VALUES(?,?,?,?,?,?)",
                (
                    connection_ref,
                    (now + timedelta(seconds=next_check_seconds)).isoformat(),
                    None,
                    "github-authorization-monitor" if has_lease else None,
                    1 if has_lease else 0,
                    (now + timedelta(seconds=30)).isoformat() if has_lease else None,
                ),
            )
        if connection_ref == in_flight_ref:
            connection.execute(
                "INSERT INTO connector_invocations "
                "(organization_id,workspace_id,actor_issuer,actor_subject,request_id,connection_ref,"
                "provider_id,profile_ref,operation_id,resource_ref,arguments_sha256,envelope_sha256,"
                "state,grant_ref,is_mutating,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    actor["organizationId"],
                    actor["workspaceId"],
                    actor["issuer"],
                    actor["subject"],
                    f"github-call-{index}",
                    connection_ref,
                    "github",
                    "github-profile",
                    "github.repository.get",
                    "configured-repository",
                    "a" * 64,
                    "b" * 64,
                    "IN_PROGRESS",
                    "sha256:" + "c" * 64,
                    0,
                    int(now.timestamp()),
                    0,
                ),
            )
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    ("blocker", "verified"),
    [
        (None, True),
        ("monitor-lease", False),
        ("in-flight-call", False),
        ("short-window", False),
        ("missing-monitor", False),
    ],
)
def test_github_issued_monitor_guard_checks_all_ten_exact_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str | None,
    verified: bool,
) -> None:
    """Probe all ten GitHub monitor states and fail closed on any missing or active row."""

    connection_refs = [f"10000000-0000-4000-8000-{index:012d}" for index in range(1, 11)]
    actor = {
        "issuer": "https://login.microsoftonline.com/tenant/v2.0",
        "subject": "actor-subject",
        "organizationId": "organization-id",
        "workspaceId": "workspace-id",
    }
    active_monitor_ref = connection_refs[0] if blocker == "monitor-lease" else None
    in_flight_ref = connection_refs[4] if blocker == "in-flight-call" else None
    missing_monitor_ref = connection_refs[9] if blocker == "missing-monitor" else None
    next_check_seconds = 300 if blocker == "short-window" else 900
    database_path = tmp_path / "integrations.sqlite3"
    _create_github_monitor_database(
        database_path,
        connection_refs=connection_refs,
        actor=actor,
        next_check_seconds=next_check_seconds,
        active_monitor_ref=active_monitor_ref,
        in_flight_ref=in_flight_ref,
        missing_monitor_ref=missing_monitor_ref,
    )
    bindings = [{"providerId": "github", "connectionRef": ref} for ref in connection_refs]
    monkeypatch.setattr(
        stage_transition, "_candidate_config_json", lambda _compose, _name: {"bindings": bindings}
    )
    monkeypatch.setattr(
        stage_transition.preflight, "_connector_bootstrap_actor", lambda _compose: actor
    )
    phases: list[str] = []

    def run_readonly_exchange_script(argv: list[str], **_kwargs: Any) -> str:
        script_index = argv.index("-c")
        script = argv[script_index + 1]
        arguments = list(argv[script_index + 2 :])
        assert json.loads(arguments[1]) == [
            {"providerId": "github", "connectionRef": ref} for ref in connection_refs
        ]
        assert arguments[-1] == "false"
        arguments[0] = str(database_path)
        result = subprocess.run(
            [sys.executable, "-c", script, *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise runtime_update.RuntimeUpdateError("read-only GitHub monitor probe failed")
        return result.stdout

    monkeypatch.setattr(runtime_update, "_checked_stdout", run_readonly_exchange_script)
    monkeypatch.setattr(
        stage_transition,
        "_write_journal",
        lambda _path, _journal, phase: phases.append(phase),
    )
    plan = {
        "phase": "issued-connector-activation",
        "imageUpdateTarget": "github-issues",
        "activeServices": {"exchange": {"containerId": "a" * 64}},
        "policy": {"healthTimeoutSeconds": 300},
    }
    journal = {
        "exchangeReadiness": {"receipt": {"githubConnectionRefs": connection_refs}},
        "githubIssuesImageUpdate": {"required": True},
    }

    if blocker == "missing-monitor":
        with pytest.raises(
            stage_transition.StageTransitionError,
            match="could not be verified read-only",
        ):
            stage_transition._verify_provider_monitor_quiescence(
                plan=plan,
                candidate_compose={"services": {}},
                journal=journal,
                journal_path=tmp_path / "journal.json",
                docker_prefix=["docker"],
            )
        assert phases == []
        return

    if verified:
        result = stage_transition._verify_provider_monitor_quiescence(
            plan=plan,
            candidate_compose={"services": {}},
            journal=journal,
            journal_path=tmp_path / "journal.json",
            docker_prefix=["docker"],
        )
        assert result["verified"] is True
        assert result["requiredWindowSeconds"] == 360
        assert journal["githubIssuesImageUpdate"]["monitorQuiescence"] == result
        assert phases == ["CONNECTOR_MONITOR_QUIESCENCE_VERIFIED"]
    else:
        with pytest.raises(stage_transition.StageTransitionError, match="blocked because"):
            stage_transition._verify_provider_monitor_quiescence(
                plan=plan,
                candidate_compose={"services": {}},
                journal=journal,
                journal_path=tmp_path / "journal.json",
                docker_prefix=["docker"],
            )
        assert journal["githubIssuesImageUpdate"]["monitorQuiescence"]["verified"] is False
        assert phases == ["CONNECTOR_MONITOR_NOT_QUIESCENT"]


def test_github_monitor_guard_rejects_connection_binding_drift_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not probe a GitHub monitor unless all ten live refs join the frozen bindings."""

    connection_refs = [f"10000000-0000-4000-8000-{index:012d}" for index in range(1, 11)]
    bindings = [{"providerId": "github", "connectionRef": ref} for ref in connection_refs]
    bindings[-1]["connectionRef"] = "20000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(
        stage_transition,
        "_candidate_config_json",
        lambda _compose, _name: {"bindings": bindings},
    )
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: pytest.fail(
            "monitor SQL must not run for a connection set outside the frozen bindings"
        ),
    )
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="connection refs do not match the frozen candidate bindings",
    ):
        stage_transition._verify_provider_monitor_quiescence(
            plan={
                "phase": "issued-connector-activation",
                "imageUpdateTarget": "github-issues",
                "candidatePreflight": {"expectedProviderScopeDigests": {"huggingface": "a" * 64}},
                "activeServices": {"exchange": {"containerId": "a" * 64}},
                "policy": {"healthTimeoutSeconds": 300},
            },
            candidate_compose={"services": {}},
            journal={
                "exchangeReadiness": {"receipt": {"githubConnectionRefs": connection_refs}},
                "githubIssuesImageUpdate": {"required": True},
            },
            journal_path=Path("/private/journal.json"),
            docker_prefix=["docker"],
        )


@pytest.mark.parametrize(
    ("blocker", "include_runpod", "verified"),
    [
        (None, False, True),
        (None, True, True),
        ("monitor-lease", False, False),
        ("in-flight-call", False, False),
        ("short-window", False, False),
        ("cloud-refresh", False, False),
    ],
)
def test_cloud_issued_monitor_guard_checks_only_configured_bindings_and_restart_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str | None,
    include_runpod: bool,
    verified: bool,
) -> None:
    """Run the Cloud idle SQL against Exchange and include RunPod only when configured."""

    all_refs = {
        "azure": "e5e1464b-00ec-4c85-9e71-1d52eab44a38",
        "gcp": "8c473baa-3d22-4ab4-9d0e-f1086b54a400",
        "runpod": "d0b0b832-a81c-4c39-94c9-d92405f2e6c2",
    }
    providers = ["azure", "gcp", *(["runpod"] if include_runpod else [])]
    connection_refs = {provider: all_refs[provider] for provider in providers}
    next_check_seconds = {provider: 900 for provider in providers}
    active_monitor_leases: set[str] = set()
    in_flight_calls: dict[str, int] = {}
    cloud_refresh_states: list[str] = []
    if blocker == "monitor-lease":
        active_monitor_leases.add("azure")
    elif blocker == "in-flight-call":
        in_flight_calls["gcp"] = 1
    elif blocker == "short-window":
        next_check_seconds["gcp"] = 300
    elif blocker == "cloud-refresh":
        cloud_refresh_states.append("QUEUED")

    actor = {
        "issuer": "https://login.microsoftonline.com/tenant/v2.0",
        "subject": "actor-subject",
        "organizationId": "organization-id",
        "workspaceId": "workspace-id",
    }
    database_path = tmp_path / "integrations.sqlite3"
    _create_cloud_monitor_database(
        database_path,
        connection_refs=connection_refs,
        actor=actor,
        next_check_seconds=next_check_seconds,
        active_monitor_leases=active_monitor_leases,
        in_flight_calls=in_flight_calls,
        cloud_refresh_states=cloud_refresh_states,
    )
    bindings = [
        {"providerId": provider, "connectionRef": connection_refs[provider]}
        for provider in providers
    ]
    monkeypatch.setattr(
        stage_transition,
        "_candidate_config_json",
        lambda _compose, _name: {"bindings": bindings},
    )
    monkeypatch.setattr(
        stage_transition.preflight, "_connector_bootstrap_actor", lambda _compose: actor
    )
    phases: list[str] = []

    def run_readonly_exchange_script(argv: list[str], **_kwargs: Any) -> str:
        script_index = argv.index("-c")
        script = argv[script_index + 1]
        arguments = list(argv[script_index + 2 :])
        assert json.loads(arguments[1]) == [
            {"providerId": provider, "connectionRef": connection_refs[provider]}
            for provider in providers
        ]
        assert arguments[-1] == "true"
        arguments[0] = str(database_path)
        result = subprocess.run(
            [sys.executable, "-c", script, *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise runtime_update.RuntimeUpdateError("read-only Exchange Cloud probe failed")
        return result.stdout

    monkeypatch.setattr(runtime_update, "_checked_stdout", run_readonly_exchange_script)
    monkeypatch.setattr(
        stage_transition,
        "_write_journal",
        lambda _path, _journal, phase: phases.append(phase),
    )
    plan = {
        "phase": "issued-connector-activation",
        "imageUpdateTarget": "cloud-management-phase2",
        "candidatePreflight": {
            "expectedProviderScopeDigests": {
                **{provider: f"{index:x}" * 64 for index, provider in enumerate(providers, 1)},
                "huggingface": "f" * 64,
            }
        },
        "activeServices": {"exchange": {"containerId": "a" * 64}},
        "policy": {"healthTimeoutSeconds": 300},
    }
    journal = {
        "exchangeReadiness": {"receipt": {"connectionRefByProvider": connection_refs}},
        "cloudImageUpdate": {"required": True},
    }

    if verified:
        result = stage_transition._verify_provider_monitor_quiescence(
            plan=plan,
            candidate_compose={"services": {}},
            journal=journal,
            journal_path=tmp_path / "journal.json",
            docker_prefix=["docker"],
        )
        assert result["verified"] is True
        assert result["requiredWindowSeconds"] == 360
        assert result["cloudRefreshNonterminalCount"] == 0
        assert journal["cloudImageUpdate"]["monitorQuiescence"] == result
        assert phases == ["CONNECTOR_MONITOR_QUIESCENCE_VERIFIED"]
    else:
        with pytest.raises(stage_transition.StageTransitionError, match="blocked because"):
            stage_transition._verify_provider_monitor_quiescence(
                plan=plan,
                candidate_compose={"services": {}},
                journal=journal,
                journal_path=tmp_path / "journal.json",
                docker_prefix=["docker"],
            )
        assert journal["cloudImageUpdate"]["monitorQuiescence"]["verified"] is False
        assert phases == ["CONNECTOR_MONITOR_NOT_QUIESCENT"]


def test_cloud_issued_monitor_guard_rejects_connection_binding_drift_before_read_or_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live Azure/GCP reference must join one frozen binding before any target mutation."""

    live_refs = {
        "azure": "e5e1464b-00ec-4c85-9e71-1d52eab44a38",
        "gcp": "8c473baa-3d22-4ab4-9d0e-f1086b54a400",
    }
    attempted: list[str] = []
    monkeypatch.setattr(
        stage_transition,
        "_candidate_config_json",
        lambda _compose, _name: {
            "bindings": [
                {"providerId": "azure", "connectionRef": "d0b0b832-a81c-4c39-94c9-d92405f2e6c2"},
                {"providerId": "gcp", "connectionRef": live_refs["gcp"]},
            ]
        },
    )
    monkeypatch.setattr(
        runtime_update,
        "_checked_stdout",
        lambda *_args, **_kwargs: attempted.append("read") or "{}",
    )
    monkeypatch.setattr(
        stage_transition,
        "_compose_target_update",
        lambda **_kwargs: attempted.append("mutate"),
    )
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="does not match its single frozen candidate binding",
    ):
        stage_transition._verify_provider_monitor_quiescence(
            plan={
                "phase": "issued-connector-activation",
                "imageUpdateTarget": "cloud-management-phase2",
                "candidatePreflight": {
                    "expectedProviderScopeDigests": {"azure": "a" * 64, "gcp": "b" * 64}
                },
                "activeServices": {"exchange": {"containerId": "a" * 64}},
                "policy": {"healthTimeoutSeconds": 300},
            },
            candidate_compose={"services": {}},
            journal={
                "exchangeReadiness": {"receipt": {"connectionRefByProvider": live_refs}},
                "cloudImageUpdate": {"required": True},
            },
            journal_path=Path("/private/journal.json"),
            docker_prefix=["docker"],
        )
    assert attempted == []


def test_exchange_readiness_hf_profile_scope_is_order_insensitive_but_exact(
    tmp_path: Path,
) -> None:
    """Treat repository order as nonsemantic while preserving permissions and duplicate checks."""

    snapshot, compose = _exchange_readiness_fixture(tmp_path)
    hf_profile = next(row for row in snapshot["profiles"] if row["providerId"] == "huggingface")
    second_repository = {
        "repoType": "dataset",
        "repoId": "example-org/private-dataset",
        "permissions": ["read"],
    }
    hf_profile["targetScope"]["repositories"].insert(0, second_repository)
    exchange_service = compose["services"]["exchange"]
    config_variable = "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"
    exchange_config = json.loads(exchange_service["environment"][config_variable])
    exchange_hf_profile = exchange_config["authorizationCloud"]["profiles"]["hf-primary"]
    exchange_hf_profile["accountScopes"]["repositories"].append(
        {
            "repoType": "dataset",
            "repoId": "example-org/private-dataset",
            "permissions": ["read"],
        }
    )
    exchange_service["environment"][config_variable] = json.dumps(exchange_config)

    receipt = stage_transition._verify_exchange_readiness_snapshot(
        snapshot=snapshot,
        compose=compose,
    )
    assert receipt["status"] == "LIVE_VERIFIED"

    changed_permissions = copy.deepcopy(snapshot)
    changed_hf_profile = next(
        row for row in changed_permissions["profiles"] if row["providerId"] == "huggingface"
    )
    changed_repository = next(
        row
        for row in changed_hf_profile["targetScope"]["repositories"]
        if row["repoType"] == "dataset"
    )
    changed_repository["permissions"] = ["read", "write"]
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="live Account Center HF profile scope/access mode differs",
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=changed_permissions,
            compose=compose,
        )

    duplicate_repository = copy.deepcopy(snapshot)
    duplicate_hf_profile = next(
        row for row in duplicate_repository["profiles"] if row["providerId"] == "huggingface"
    )
    duplicate_hf_profile["targetScope"]["repositories"].append(copy.deepcopy(second_repository))
    with pytest.raises(
        stage_transition.StageTransitionError,
        match="repository scope is invalid",
    ):
        stage_transition._verify_exchange_readiness_snapshot(
            snapshot=duplicate_repository,
            compose=compose,
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


def test_github_stage_transition_reuses_the_existing_allowlisted_compose_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the V2 transaction while using the existing GitHub-only service wrapper."""

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runtime_update,
        "_compose_up",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        runtime_update,
        "_compose_up_stage_transition",
        lambda **_kwargs: pytest.fail(
            "GitHub is intentionally outside the general stage-transition service order"
        ),
    )
    stage_transition._compose_target_update(
        plan={
            "project": "cyrene-teams-cloud",
            "candidateInputs": {
                "composeFiles": [{"path": "/private/compose.yaml"}],
                "envFiles": [{"path": "/private/runtime.env"}],
            },
            "policy": {"healthTimeoutSeconds": 120},
        },
        service="github-issues",
        use_candidate=True,
        docker_prefix=["docker"],
    )

    assert calls == [
        {
            "docker_prefix": ["docker"],
            "project": "cyrene-teams-cloud",
            "compose_files": [Path("/private/compose.yaml")],
            "env_files": [Path("/private/runtime.env")],
            "service": "github-issues",
            "timeout_seconds": 120,
        }
    ]


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
