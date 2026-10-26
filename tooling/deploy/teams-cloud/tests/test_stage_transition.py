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
import importlib
import json
import stat
import sys
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
    candidate_services["cloud-management-phase2"] = _service("cloud:v1", profiles=["runpod"])
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


def test_sqlite_migration_rejects_removed_schema_and_never_restores_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep rollback image-only when candidate persistence removes an existing schema object."""

    monkeypatch.setattr(
        stage_transition,
        "_live_sqlite_report",
        lambda **_kwargs: {
            "integrity": "ok",
            "userVersion": 2,
            "schemaSha256": "a" * 64,
            "objects": ["table:new_table"],
        },
    )
    plan = {
        "operationId": "00000000-0000-4000-8000-000000000001",
        "database": {"containerPath": "/var/lib/cyrene-navigator/persistence.db"},
    }
    journal = {
        "database": {
            "snapshot": {
                "integrity": "ok",
                "userVersion": 1,
                "objects": ["table:work_items"],
            },
            "restorePerformed": False,
        },
        "targetProgress": {"navigator-persistence": {"candidateContainerId": "1" * 64}},
        "history": [],
    }
    journal_path = tmp_path / "journal.json"

    with pytest.raises(stage_transition.StageTransitionError, match="removed schema objects"):
        stage_transition._validate_database_migration(
            plan=plan,
            journal=journal,
            journal_path=journal_path,
            docker_prefix=["docker"],
        )
    assert journal["database"]["restorePerformed"] is False


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
    with pytest.raises(runtime_update.RuntimeUpdateError, match="outside the formal"):
        runtime_update._compose_up_stage_transition(
            docker_prefix=["docker"],
            project="cyrene-teams-cloud",
            compose_files=[Path("compose.yaml")],
            env_files=[Path("runtime.env")],
            service="second-navigator",
            timeout_seconds=60,
        )
