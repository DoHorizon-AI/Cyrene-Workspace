"""Validate the disabled-by-default automation deployment fragment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = DEPLOYMENT_ROOT / "config"


def _read_schema(name: str) -> dict[str, Any]:
    value = json.loads((CONFIG_ROOT / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(value)
    return value


def _valid_source_bindings() -> dict[str, Any]:
    actor = "11111111-1111-4111-8111-111111111111"
    return {
        "schemaVersion": 1,
        "targets": [
            {
                "actorId": actor,
                "targetId": "yield-run-current",
                "kind": "training_run",
                "bindingId": "22222222-2222-4222-8222-222222222222",
                "product": "yield",
                "resourceKind": "training-run",
                "resourceId": "33333333-3333-4333-8333-333333333333",
                "organizationId": "44444444-4444-4444-8444-444444444444",
                "workspaceId": "55555555-5555-4555-8555-555555555555",
            },
            {
                "actorId": actor,
                "targetId": "dataset-current",
                "kind": "product_resource",
                "bindingId": "66666666-6666-4666-8666-666666666666",
                "product": "catalyst",
                "resourceKind": "dataset-version",
                "resourceId": "77777777-7777-4777-8777-777777777777",
                "organizationId": "44444444-4444-4444-8444-444444444444",
                "workspaceId": "55555555-5555-4555-8555-555555555555",
            },
            {
                "actorId": actor,
                "targetId": "vm-health-current",
                "kind": "host_health",
                "bindingId": "88888888-8888-4888-8888-888888888888",
                "hostRef": "pilot-vm",
                "mountDirectory": "/run/cyrene/automation-health",
                "mountGid": 10001,
                "maxAgeSeconds": 120,
                "maxClockSkewSeconds": 10,
            },
            {
                "actorId": actor,
                "targetId": "planner-target-alias",
                "kind": "planner_plan",
                "bindingId": "99999999-9999-4999-8999-999999999999",
                "connectionRef": "conn_ref_01",
                "profileRef": "profile_ref_01",
                "tenantId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "planId": "plan_01",
            },
        ],
    }


def test_workflow_overlay_defaults_disabled_and_only_adds_private_credentials() -> None:
    overlay = yaml.safe_load(
        (DEPLOYMENT_ROOT / "compose.automation-workflows.yaml").read_text(encoding="utf-8")
    )
    base = yaml.safe_load((DEPLOYMENT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert set(overlay["services"]) == {"navigator", "navigator-persistence", "teams-ingress"}
    assert set(overlay["services"]) <= set(base["services"])
    assert overlay["services"]["teams-ingress"] == {
        "environment": {
            "TEAMS_PERSONAL_CHAT_ENABLED": "${CYRENE_TEAMS_PERSONAL_CHAT_ENABLED:-false}"
        }
    }
    assert overlay["services"]["navigator"]["environment"]["CYRENE_WORKFLOWS_ENABLED"] == (
        "${CYRENE_WORKFLOWS_ENABLED:-false}"
    )
    assert overlay["services"]["navigator"]["environment"][
        "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED"
    ] == ("${CYRENE_TEAMS_PERSONAL_CHAT_ENABLED:-false}")
    navigator_environment = overlay["services"]["navigator"]["environment"]
    assert navigator_environment["CYRENE_WORK_ACTOR_TOKEN_MAP_FILE"] == (
        "/run/teams-authorization/work-actor-token-map.json"
    )
    assert navigator_environment["CYRENE_AUTOMATION_SOURCE_BINDINGS_FILE"] == (
        "/run/teams-authorization/automation-source-bindings.json"
    )
    base_mounts = base["services"]["navigator"]["volumes"]
    assert any(
        isinstance(mount, dict)
        and mount.get("target") == "/run/teams-authorization"
        and mount.get("read_only") is True
        for mount in base_mounts
    )
    health_mounts = [
        mount
        for mount in overlay["services"]["navigator"]["volumes"]
        if isinstance(mount, dict) and mount.get("target") == "/run/cyrene/automation-health"
    ]
    assert len(health_mounts) == 1
    assert health_mounts[0]["read_only"] is True
    assert health_mounts[0]["bind"]["create_host_path"] is False

    work_token_envs = {f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}" for index in range(1, 6)}
    mappings = navigator_environment["CYRENE_SECRET_FILE_MAP"].split(",")
    mapped_envs = {mapping.partition("=")[0] for mapping in mappings}
    assert work_token_envs <= mapped_envs
    assert {
        "CYRENE_SESSION_TOKEN",
        "TEAMS_NAVIGATOR_HMAC_KEY",
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
    } <= mapped_envs
    assert len(mapped_envs) == len(mappings)
    persistence_environment = overlay["services"]["navigator-persistence"]["environment"]
    assert persistence_environment["TEAMS_TASK_ACTOR_SUBJECTS_FILE"] == (
        "/run/teams-authorization/teams-actor-subjects.json"
    )
    persistence_auth_mount = overlay["services"]["navigator-persistence"]["volumes"][0]
    assert persistence_auth_mount["target"] == "/run/teams-authorization"
    assert persistence_auth_mount["read_only"] is True
    assert persistence_auth_mount["bind"]["create_host_path"] is False
    persistence_mappings = persistence_environment["CYRENE_SECRET_FILE_MAP"].split(",")
    persistence_envs = {mapping.partition("=")[0] for mapping in persistence_mappings}
    assert work_token_envs <= persistence_envs
    assert {
        "NAVIGATOR_SESSION_TOKEN",
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
    } <= persistence_envs
    assert len(persistence_envs) == len(persistence_mappings)

    bridge_consumers = {
        service_name
        for service_name, service in overlay["services"].items()
        if any(
            secret.get("source") == "teams-private-bridge-token"
            for secret in service.get("secrets", [])
        )
    }
    assert bridge_consumers == {"navigator", "navigator-persistence"}
    actor_consumers = {
        service_name
        for service_name, service in overlay["services"].items()
        if any(
            secret.get("source", "").startswith("work-actor-token-")
            for secret in service.get("secrets", [])
        )
    }
    assert actor_consumers == {"navigator", "navigator-persistence"}


def test_actor_token_schema_accepts_slot_references_and_rejects_token_values() -> None:
    schema = _read_schema("work-actor-token-map.schema.json")
    checker = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    valid = {
        "schemaVersion": 1,
        "workspaceId": "11111111-1111-4111-8111-111111111111",
        "actors": [
            {
                "actorAadObjectId": "22222222-2222-4222-8222-222222222222",
                "tokenEnv": "CYRENE_WORK_ACTOR_TOKEN_01",
            },
            {
                "actorAadObjectId": "33333333-3333-4333-8333-333333333333",
                "tokenEnv": "CYRENE_WORK_ACTOR_TOKEN_02",
            },
            {
                "actorAadObjectId": "44444444-4444-4444-8444-444444444444",
                "tokenEnv": "CYRENE_WORK_ACTOR_TOKEN_03",
            },
            {
                "actorAadObjectId": "55555555-5555-4555-8555-555555555555",
                "tokenEnv": "CYRENE_WORK_ACTOR_TOKEN_04",
            },
            {
                "actorAadObjectId": "66666666-6666-4666-8666-666666666666",
                "tokenEnv": "CYRENE_WORK_ACTOR_TOKEN_05",
            },
        ],
    }
    checker.validate(valid)
    owner_only = {**valid, "actors": valid["actors"][:1]}
    checker.validate(owner_only)
    invalid = {
        **valid,
        "actors": [
            {**valid["actors"][0], "token": "synthetic-token"},
            *valid["actors"][1:],
        ],
    }
    assert list(checker.iter_errors(invalid))
    invalid_slot = {
        **valid,
        "actors": [
            {**valid["actors"][0], "tokenEnv": "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN"},
            *valid["actors"][1:],
        ],
    }
    assert list(checker.iter_errors(invalid_slot))
    empty_slots = {**valid, "actors": []}
    assert list(checker.iter_errors(empty_slots))
    noncontiguous_slots = {
        **valid,
        "actors": [
            {**valid["actors"][1], "actorAadObjectId": valid["actors"][0]["actorAadObjectId"]}
        ],
    }
    assert list(checker.iter_errors(noncontiguous_slots))


def test_principal_patch_accepts_one_to_five_private_writers_and_separate_bridge() -> None:
    schema = _read_schema("automation-work-principals.patch.schema.json")
    checker = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    template = json.loads(
        (CONFIG_ROOT / "automation-work-principals.patch.template.json").read_text(encoding="utf-8")
    )
    checker.validate(template)
    assert "principals" not in template
    assert "CYRENE_SESSION_TOKEN" not in json.dumps(template)
    assert "NAVIGATOR_SESSION_TOKEN" not in json.dumps(template)

    writers = template["privateWriterPrincipals"]
    owner_only = json.loads(json.dumps(template))
    owner_only["privateWriterPrincipals"] = writers[:1]
    checker.validate(owner_only)
    assert [row["token_env"] for row in writers] == [
        f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}" for index in range(1, 6)
    ]
    assert all(row["can_takeover"] is True for row in writers)
    assert all(row["actor_private_scope"] is True for row in writers)
    assert all(row["can_write_harness"] is True for row in writers)
    assert all(row["can_publish_credential_health"] is False for row in writers)
    assert all(row["can_bind_teams_private_conversations"] is False for row in writers)
    assert all(len(row["workspace_ids"]) == 1 for row in writers)
    assert len({row["workspace_ids"][0] for row in writers}) == 1
    assert len({row["organization_id"] for row in writers}) == 1

    bridge = template["privateTeamsBridgePrincipal"]
    assert bridge["token_env"] == "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN"
    assert bridge["actor_id"] == "teams-private-bridge"
    assert bridge["can_bind_teams_private_conversations"] is True
    assert all(
        bridge[field] is False
        for field in (
            "can_takeover",
            "actor_private_scope",
            "can_write_harness",
            "can_publish_credential_health",
        )
    )

    missing_writer = json.loads(json.dumps(template))
    missing_writer["privateWriterPrincipals"] = []
    assert list(checker.iter_errors(missing_writer))
    shared_writer = json.loads(json.dumps(template))
    shared_writer["privateWriterPrincipals"][1]["actor_private_scope"] = False
    assert list(checker.iter_errors(shared_writer))
    elevated_bridge = json.loads(json.dumps(template))
    elevated_bridge["privateTeamsBridgePrincipal"]["can_takeover"] = True
    assert list(checker.iter_errors(elevated_bridge))
    unusable_writer = json.loads(json.dumps(template))
    unusable_writer["privateWriterPrincipals"][2]["can_write_harness"] = False
    assert list(checker.iter_errors(unusable_writer))


def test_source_binding_schema_accepts_fixed_rows_and_rejects_grants_or_arbitrary_urls() -> None:
    schema = _read_schema("automation-source-bindings.schema.json")
    checker = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    valid = _valid_source_bindings()
    checker.validate(valid)

    with_grant = json.loads(json.dumps(valid))
    with_grant["targets"][0]["sourceAccessVerified"] = True
    assert list(checker.iter_errors(with_grant))

    with_url = json.loads(json.dumps(valid))
    with_url["targets"][3]["baseUrl"] = "https://example.invalid/"
    assert list(checker.iter_errors(with_url))

    incompatible_product = json.loads(json.dumps(valid))
    incompatible_product["targets"][1]["resourceKind"] = "deployment"
    assert list(checker.iter_errors(incompatible_product))
