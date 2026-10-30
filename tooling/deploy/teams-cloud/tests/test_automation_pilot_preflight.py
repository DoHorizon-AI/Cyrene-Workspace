"""Check private pilot preflight gates using synthetic actors and source bindings."""

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

DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOYMENT_ROOT))
preflight = importlib.import_module("preflight")

OWNER = "11111111-1111-4111-8111-111111111111"
ACTORS = [
    OWNER,
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
]
ORGANIZATION = "66666666-6666-4666-8666-666666666666"
WORKSPACE = "77777777-7777-4777-8777-777777777777"
TENANT = "88888888-8888-4888-8888-888888888888"


def _principals() -> list[dict[str, Any]]:
    rows = [
        {
            "token_env": "NAVIGATOR_SESSION_TOKEN",
            "actor_id": OWNER,
            "workspace_ids": [WORKSPACE],
            "organization_id": ORGANIZATION,
            "can_takeover": True,
            "can_write_harness": False,
            "can_publish_credential_health": False,
        }
    ]
    for index, actor in enumerate(ACTORS, start=1):
        rows.append(
            {
                "token_env": f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}",
                "actor_id": actor,
                "workspace_ids": [WORKSPACE],
                "organization_id": ORGANIZATION,
                "can_takeover": True,
                "can_write_harness": True,
                "actor_private_scope": True,
                "can_publish_credential_health": False,
                "can_bind_teams_private_conversations": False,
            }
        )
    rows.append(
        {
            "token_env": "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
            "actor_id": "teams-private-bridge",
            "workspace_ids": [WORKSPACE],
            "organization_id": ORGANIZATION,
            "can_takeover": False,
            "can_write_harness": False,
            "actor_private_scope": False,
            "can_publish_credential_health": False,
            "can_bind_teams_private_conversations": True,
        }
    )
    return rows


def test_empty_optional_source_registry_does_not_grant_any_target() -> None:
    """Pilot identity preparation can leave Office and training explicitly disabled."""

    assert (
        preflight._validate_automation_source_bindings(
            {"schemaVersion": 1, "targets": []},
            actor_ids=set(ACTORS),
            connector_bindings={"schemaVersion": 1, "bindings": []},
            organization_id=ORGANIZATION,
            workspace_id=WORKSPACE,
            tenant_id=TENANT,
        )
        == []
    )


def _fixtures(tmp_path: Path) -> tuple[dict[str, Any], Path, Path, dict[str, Any]]:
    config_dir = tmp_path / "private-config"
    secrets_dir = tmp_path / "private-secrets"
    health_dir = tmp_path / "automation-health"
    config_dir.mkdir()
    secrets_dir.mkdir()
    health_dir.mkdir()
    health_dir.chmod(0o750)
    work_token_envs = [f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}" for index in range(1, 6)]
    automation_secret_names = {
        "teams-private-bridge-token",
        *(f"work-actor-token-{index:02d}" for index in range(1, 6)),
    }
    nav_secret_pairs = {
        ("navigator-exchange-hmac-key", "navigator-exchange-hmac-key"),
        ("navigator-session-token", "navigator-session-token"),
        ("teams-navigator-hmac-key", "teams-navigator-hmac-key"),
        ("internal-service-root-ca", "internal-service-root-ca"),
        *((name, name) for name in automation_secret_names),
    }
    persistence_secret_pairs = {
        ("navigator-session-token", "navigator-session-token"),
        ("navigator-persistence-tls-cert", "navigator-persistence-tls-cert"),
        ("navigator-persistence-tls-key", "navigator-persistence-tls-key"),
        ("internal-service-root-ca", "internal-service-root-ca"),
        *((name, name) for name in automation_secret_names),
    }
    navigator_map = {
        "CYRENE_SESSION_TOKEN": "/run/secrets/navigator-session-token",
        "TEAMS_NAVIGATOR_HMAC_KEY": "/run/secrets/teams-navigator-hmac-key",
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN": "/run/secrets/teams-private-bridge-token",
        **{
            env: f"/run/secrets/work-actor-token-{index:02d}"
            for index, env in enumerate(work_token_envs, start=1)
        },
    }
    persistence_map = {
        "NAVIGATOR_SESSION_TOKEN": "/run/secrets/navigator-session-token",
        "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN": "/run/secrets/teams-private-bridge-token",
        **{
            env: f"/run/secrets/work-actor-token-{index:02d}"
            for index, env in enumerate(work_token_envs, start=1)
        },
    }
    config = {
        "services": {
            "navigator": {
                "environment": {
                    "CYRENE_WORK_ACTOR_TOKEN_MAP_FILE": "/run/teams-authorization/work-actor-token-map.json",
                    "CYRENE_AUTOMATION_SOURCE_BINDINGS_FILE": "/run/teams-authorization/automation-source-bindings.json",
                    "CYRENE_WORKFLOWS_ENABLED": "false",
                    "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED": "false",
                    "CYRENE_WORKSPACE_ID": WORKSPACE,
                    "TEAMS_TENANT_ID": TENANT,
                    "CYRENE_SECRET_FILE_MAP": ",".join(
                        f"{key}={value}" for key, value in navigator_map.items()
                    ),
                },
                "secrets": [
                    {"source": name, "target": target} for name, target in sorted(nav_secret_pairs)
                ],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(health_dir),
                        "target": preflight.AUTOMATION_HEALTH_TARGET,
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    },
                    {
                        "type": "bind",
                        "source": str(config_dir),
                        "target": "/run/teams-authorization",
                        "read_only": True,
                    },
                ],
            },
            "navigator-persistence": {
                "environment": {
                    "TEAMS_TASK_ACTOR_SUBJECTS_FILE": "/run/teams-authorization/teams-actor-subjects.json",
                    "CYRENE_SECRET_FILE_MAP": ",".join(
                        f"{key}={value}" for key, value in persistence_map.items()
                    ),
                },
                "secrets": [
                    {"source": name, "target": target}
                    for name, target in sorted(persistence_secret_pairs)
                ],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(config_dir),
                        "target": "/run/teams-authorization",
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    }
                ],
            },
            "directory-operator": {
                "environment": {
                    "CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID": OWNER,
                    "CYRENE_ORGANIZATION_ID": ORGANIZATION,
                }
            },
        },
        "secrets": {name: {"file": str(secrets_dir / name)} for name in automation_secret_names},
    }
    actor_map = {
        "schemaVersion": 1,
        "workspaceId": WORKSPACE,
        "actors": [
            {"actorAadObjectId": actor, "tokenEnv": f"CYRENE_WORK_ACTOR_TOKEN_{index:02d}"}
            for index, actor in enumerate(ACTORS, start=1)
        ],
    }
    actor_projection = {
        "schemaVersion": 1,
        "workspaceId": WORKSPACE,
        "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "subjects": ACTORS.copy(),
    }
    source_bindings = {
        "schemaVersion": 1,
        "targets": [
            {
                "actorId": OWNER,
                "targetId": "vm-health-current",
                "kind": "host_health",
                "bindingId": "99999999-9999-4999-8999-999999999999",
                "hostRef": "pilot-vm",
                "mountDirectory": preflight.AUTOMATION_HEALTH_TARGET,
                "mountGid": 10001,
                "maxAgeSeconds": 120,
                "maxClockSkewSeconds": 10,
            }
        ],
    }
    documents = {
        "work-actor-token-map.json": actor_map,
        "teams-actor-subjects.json": actor_projection,
        "automation-source-bindings.json": source_bindings,
        "connector-bindings.json": {"schemaVersion": 1, "bindings": []},
        "navigator-principal-config.json": {
            "principals": _principals(),
            "product_directory": [],
            "product_service_credentials": [],
        },
    }
    return config, config_dir, secrets_dir, documents


def _patch_private_document_loader(
    monkeypatch: pytest.MonkeyPatch, documents: dict[str, Any]
) -> None:
    def read_document(path: Path, _name: str, *, maximum_bytes: int = 1_000_000) -> Any:
        del maximum_bytes
        return copy.deepcopy(documents[path.name])

    monkeypatch.setattr(preflight, "_private_runtime_json", read_document)


def _patch_health_directory_stat(monkeypatch: pytest.MonkeyPatch, health_dir: Path) -> None:
    original_lstat = Path.lstat

    def lstat(path: Path) -> Any:
        if path == health_dir:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o750,
                st_uid=0,
                st_gid=10001,
            )
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat)


def test_private_pilot_preflight_binds_five_directory_actors_and_safe_source_selectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, config_dir, secrets_dir, documents = _fixtures(tmp_path)
    _patch_private_document_loader(monkeypatch, documents)
    _patch_health_directory_stat(
        monkeypatch, Path(config["services"]["navigator"]["volumes"][0]["source"])
    )

    assert preflight._validate_automation_workflow_configuration(
        config, config_dir=config_dir, secrets_dir=secrets_dir
    )


def test_pilot_cannot_assign_existing_model_authorization_to_another_member(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, config_dir, secrets_dir, documents = _fixtures(tmp_path)
    _patch_private_document_loader(monkeypatch, documents)
    config["services"]["navigator"]["environment"]["CYRENE_TEAMS_MODEL_ACTOR_SUBJECT"] = ACTORS[1]
    with pytest.raises(preflight.PreflightError, match="model authorization"):
        preflight._validate_automation_workflow_configuration(
            config, config_dir=config_dir, secrets_dir=secrets_dir
        )


def test_personal_transport_requires_matching_work_policy_bot_and_actor_targets(
    tmp_path: Path,
) -> None:
    """An enabled Navigator alone cannot admit a half-configured private transport."""

    config, config_dir, _secrets, _documents = _fixtures(tmp_path)
    bot = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    config["services"]["navigator"]["environment"].update(
        CYRENE_TEAMS_PERSONAL_CHAT_ENABLED="true", TEAMS_BOT_APP_ID=bot
    )
    config["services"]["teams-ingress"] = {"environment": {"TEAMS_PERSONAL_CHAT_ENABLED": "true"}}
    config["services"]["teams-provider"] = {
        "environment": {
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
                {
                    "bot_app_id": bot,
                    "tenant_id": TENANT,
                    "personal_chat_enabled": True,
                }
            )
        }
    }
    profile = {
        "botAppId": bot,
        "expectedIssuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "workspaceId": WORKSPACE,
        "personalActorSubjects": ACTORS.copy(),
    }
    config["services"]["exchange"] = {
        "environment": {
            "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                {
                    "connectorHost": {"teamsMessages": profile},
                }
            ),
        }
    }
    principal_document = {
        "teams_private_conversations": {
            "actor_subjects_file_env": "TEAMS_TASK_ACTOR_SUBJECTS_FILE",
            "workspace_id": WORKSPACE,
            "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "tenant_id": TENANT,
            "bot_app_id": bot,
        }
    }
    preflight._validate_automation_personal_delivery(
        config,
        config_dir=config_dir,
        actor_ids=set(ACTORS),
        tenant_id=TENANT,
        principal_document=principal_document,
    )
    config["services"]["teams-ingress"]["environment"]["TEAMS_PERSONAL_CHAT_ENABLED"] = "false"
    with pytest.raises(preflight.PreflightError, match="personal Teams transport"):
        preflight._validate_automation_personal_delivery(
            config,
            config_dir=config_dir,
            actor_ids=set(ACTORS),
            tenant_id=TENANT,
            principal_document=principal_document,
        )
    config["services"]["teams-ingress"]["environment"]["TEAMS_PERSONAL_CHAT_ENABLED"] = "true"
    profile["personalActorSubjects"] = [OWNER]
    config["services"]["exchange"]["environment"][
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"
    ] = json.dumps(
        {
            "connectorHost": {"teamsMessages": profile},
        }
    )
    with pytest.raises(preflight.PreflightError, match="personal Teams transport"):
        preflight._validate_automation_personal_delivery(
            config,
            config_dir=config_dir,
            actor_ids=set(ACTORS),
            tenant_id=TENANT,
            principal_document=principal_document,
        )


def test_graph_bootstrap_requires_owner_only_personal_chat_without_enabling_workflows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, config_dir, secrets_dir, documents = _fixtures(tmp_path)
    health_dir = Path(config["services"]["navigator"]["volumes"][0]["source"])
    _patch_private_document_loader(monkeypatch, documents)
    _patch_health_directory_stat(monkeypatch, health_dir)
    bot = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    navigator = config["services"]["navigator"]["environment"]
    navigator.update(
        CYRENE_TEAMS_PERSONAL_CHAT_ENABLED="true",
        CYRENE_TEAMS_MODEL_ACTOR_SUBJECT=OWNER,
        TEAMS_BOT_APP_ID=bot,
    )
    config["services"]["teams-ingress"] = {
        "environment": {"TEAMS_PERSONAL_CHAT_ENABLED": "true"}
    }
    config["services"]["teams-provider"] = {
        "environment": {
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
                {"bot_app_id": bot, "tenant_id": TENANT, "personal_chat_enabled": True}
            )
        }
    }
    profile = {
        "botAppId": bot,
        "expectedIssuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "workspaceId": WORKSPACE,
        "personalActorSubjects": ACTORS.copy(),
    }
    config["services"]["exchange"] = {
        "environment": {
            "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                {"connectorHost": {"teamsMessages": profile}}
            )
        }
    }
    documents["navigator-principal-config.json"]["teams_private_conversations"] = {
        "actor_subjects_file_env": "TEAMS_TASK_ACTOR_SUBJECTS_FILE",
        "workspace_id": WORKSPACE,
        "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tenant_id": TENANT,
        "bot_app_id": bot,
    }

    assert preflight._validate_automation_workflow_configuration(
        config,
        config_dir=config_dir,
        secrets_dir=secrets_dir,
        require_personal_delivery=True,
    )

    navigator["CYRENE_WORKFLOWS_ENABLED"] = "true"
    with pytest.raises(preflight.PreflightError, match="keep Workflows off"):
        preflight._validate_automation_workflow_configuration(
            config,
            config_dir=config_dir,
            secrets_dir=secrets_dir,
            require_personal_delivery=True,
        )


def test_teams_connector_personal_flag_must_match_navigator_route() -> None:
    bot = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    provider = {
        "environment": {
            "CYRENE_CAPABILITY_BINDING_ID": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
                {
                    "bot_app_id": bot,
                    "tenant_id": TENANT,
                    "allowed_team_ids": ["team-1"],
                    "client_secret_env_ref": "TEAMS_BOT_CLIENT_SECRET",
                    "personal_chat_enabled": False,
                }
            ),
            "CYRENE_SECRET_FILE_MAP": "TEAMS_BOT_CLIENT_SECRET=/run/secrets/teams-bot-client-secret",
        },
        "secrets": [{"source": "teams-bot-client-secret", "target": "teams-bot-client-secret"}],
    }
    navigator = {
        "environment": {
            "TEAMS_ALLOWED_TEAM_IDS": "team-1",
            "TEAMS_BOT_APP_ID": bot,
            "TEAMS_TENANT_ID": TENANT,
            "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED": "false",
        }
    }
    preflight._validate_teams_connector_config(provider, navigator)

    navigator["environment"]["CYRENE_TEAMS_PERSONAL_CHAT_ENABLED"] = "true"
    config = json.loads(provider["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"])
    config["personal_chat_enabled"] = True
    provider["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    preflight._validate_teams_connector_config(provider, navigator)

    config["personal_chat_enabled"] = False
    provider["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    with pytest.raises(preflight.PreflightError, match="Teams provider activation"):
        preflight._validate_teams_connector_config(provider, navigator)


def test_private_pilot_preflight_rejects_partial_actor_token_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, config_dir, secrets_dir, documents = _fixtures(tmp_path)
    documents["work-actor-token-map.json"]["actors"].pop()
    _patch_private_document_loader(monkeypatch, documents)
    _patch_health_directory_stat(
        monkeypatch, Path(config["services"]["navigator"]["volumes"][0]["source"])
    )

    with pytest.raises(preflight.PreflightError, match="exactly five"):
        preflight._validate_automation_workflow_configuration(
            config, config_dir=config_dir, secrets_dir=secrets_dir
        )


def test_revoked_member_does_not_block_remaining_pilot_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale private token slot never grants access or makes revocation an outage."""

    config, config_dir, secrets_dir, documents = _fixtures(tmp_path)
    documents["teams-actor-subjects.json"]["subjects"] = ACTORS[:-1]
    _patch_private_document_loader(monkeypatch, documents)
    _patch_health_directory_stat(
        monkeypatch, Path(config["services"]["navigator"]["volumes"][0]["source"])
    )
    assert preflight._validate_automation_workflow_configuration(
        config, config_dir=config_dir, secrets_dir=secrets_dir
    )


def test_private_pilot_preflight_rejects_unconfigured_directory_actor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, config_dir, secrets_dir, documents = _fixtures(tmp_path)
    documents["work-actor-token-map.json"]["actors"][1]["actorAadObjectId"] = (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    _patch_private_document_loader(monkeypatch, documents)
    _patch_health_directory_stat(
        monkeypatch, Path(config["services"]["navigator"]["volumes"][0]["source"])
    )

    with pytest.raises(preflight.PreflightError, match="Directory projection"):
        preflight._validate_automation_workflow_configuration(
            config, config_dir=config_dir, secrets_dir=secrets_dir
        )


def test_private_pilot_bridge_cannot_use_owner_identity_or_work_grants(tmp_path: Path) -> None:
    config, _config_dir, _secrets_dir, documents = _fixtures(tmp_path)
    bridge = documents["navigator-principal-config.json"]["principals"][-1]
    bridge["actor_id"] = OWNER
    bridge["can_takeover"] = True
    principal_path = tmp_path / "navigator-principal-config.json"
    principal_path.write_text(
        json.dumps(documents["navigator-principal-config.json"]), encoding="utf-8"
    )

    with pytest.raises(preflight.PreflightError, match="non-human identity"):
        preflight._validate_principal_configuration(principal_path, config["services"])


def test_private_pilot_preflight_rejects_writable_or_retargeted_health_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, config_dir, secrets_dir, documents = _fixtures(tmp_path)
    config["services"]["navigator"]["volumes"][0]["read_only"] = False
    _patch_private_document_loader(monkeypatch, documents)
    _patch_health_directory_stat(
        monkeypatch, Path(config["services"]["navigator"]["volumes"][0]["source"])
    )

    with pytest.raises(preflight.PreflightError, match="read-only directory"):
        preflight._validate_automation_workflow_configuration(
            config, config_dir=config_dir, secrets_dir=secrets_dir
        )


def _add_planner_source(documents: dict[str, Any]) -> None:
    connection_ref = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    profile_ref = "profile_01"
    documents["automation-source-bindings.json"]["targets"].append(
        {
            "actorId": OWNER,
            "targetId": "planner-current",
            "kind": "planner_plan",
            "bindingId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "connectionRef": connection_ref,
            "profileRef": profile_ref,
            "tenantId": TENANT,
            "planId": "plan_01",
        }
    )
    operations = (
        "microsoft_graph.planner.plan.get",
        "microsoft_graph.planner.tasks.list",
    )
    documents["connector-bindings.json"] = {
        "schemaVersion": 1,
        "bindings": [
            {
                "bindingId": f"exchange-microsoft_graph-{connection_ref}",
                "connectionRef": connection_ref,
                "providerId": "microsoft_graph",
                "profileRef": profile_ref,
                "tools": [
                    {
                        "toolId": operation,
                        "operationId": operation,
                        "resourceProjection": "microsoft_graph_plan",
                        "mutating": False,
                    }
                    for operation in operations
                ],
            }
        ],
    }


def test_planner_source_requires_plan_metadata_and_task_reads_on_same_graph_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, config_dir, secrets_dir, documents = _fixtures(tmp_path)
    _add_planner_source(documents)
    _patch_private_document_loader(monkeypatch, documents)
    _patch_health_directory_stat(
        monkeypatch, Path(config["services"]["navigator"]["volumes"][0]["source"])
    )

    assert preflight._validate_automation_workflow_configuration(
        config, config_dir=config_dir, secrets_dir=secrets_dir
    )

    documents["connector-bindings.json"]["bindings"][0]["tools"].pop(0)
    _patch_private_document_loader(monkeypatch, documents)
    with pytest.raises(preflight.PreflightError, match="both matching plan metadata and task-list"):
        preflight._validate_automation_workflow_configuration(
            config, config_dir=config_dir, secrets_dir=secrets_dir
        )


def test_graph_and_ado_processes_cannot_exceed_current_vm_cgroup_budget() -> None:
    image = "pilot:test"
    config = {
        "services": {
            "existing-runtime": {"image": image, "mem_limit": "3520m", "cpus": "1.85"},
            "microsoft-graph": {"image": image, "mem_limit": "192m", "cpus": "0.10"},
            "azure-devops-observation": {
                "image": image,
                "mem_limit": "192m",
                "cpus": "0.10",
            },
        }
    }

    with pytest.raises(preflight.PreflightError, match="memory limits total 3904 MiB"):
        preflight._validate_compose(config, "stage1", 3584, 1.95, {image})


def _provider_service(service_name: str, config: dict[str, Any]) -> dict[str, Any]:
    graph = service_name == "microsoft-graph"
    prefix = "microsoft-graph" if graph else "azure-devops-observation"
    exchange_env = (
        "EXCHANGE_GRAPH_AUTH_CONTEXT_HMAC_KEY" if graph else "EXCHANGE_ADO_AUTH_CONTEXT_HMAC_KEY"
    )
    provider_env = (
        "GRAPH_ACCOUNT_CENTER_PROVIDER_HMAC_KEY"
        if graph
        else "ADO_ACCOUNT_CENTER_PROVIDER_HMAC_KEY"
    )
    root_env = (
        "GRAPH_ACCOUNT_CENTER_ROOT_CERTIFICATE" if graph else "ADO_ACCOUNT_CENTER_ROOT_CERTIFICATE"
    )
    cert_env = (
        "GRAPH_ACCOUNT_CENTER_CLIENT_CERTIFICATE"
        if graph
        else "ADO_ACCOUNT_CENTER_CLIENT_CERTIFICATE"
    )
    key_env = "GRAPH_ACCOUNT_CENTER_CLIENT_KEY" if graph else "ADO_ACCOUNT_CENTER_CLIENT_KEY"
    exchange_secret = (
        "exchange-graph-auth-context-hmac-key" if graph else "exchange-ado-auth-context-hmac-key"
    )
    provider_secret = (
        "account-center-graph-provider-hmac-key"
        if graph
        else "account-center-ado-provider-hmac-key"
    )
    account_cert = f"{prefix}-account-center-client-cert"
    account_key = f"{prefix}-account-center-client-key"
    server_cert = f"{prefix}-server-cert"
    server_key = f"{prefix}-server-key"
    health_cert = f"{prefix}-health-client-cert"
    health_key = f"{prefix}-health-client-key"
    exchange_network = "graph-exchange-private" if graph else "ado-exchange-private"
    account_network = "graph-account-center-private" if graph else "ado-account-center-private"
    egress_network = "graph-egress" if graph else "ado-egress"
    provider_id = f"cyrene.providers.{prefix}"
    return {
        "environment": {
            "CYRENE_CAPABILITY_BINDING_ID": f"exchange-{prefix}-binding",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
            "CYRENE_SECRET_FILE_MAP": ",".join(
                (
                    f"{exchange_env}=/run/secrets/{exchange_secret}",
                    f"{provider_env}=/run/secrets/{provider_secret}",
                    f"{root_env}=/run/secrets/exchange-plugin-root-ca",
                    f"{cert_env}=/run/secrets/{account_cert}",
                    f"{key_env}=/run/secrets/{account_key}",
                )
            ),
        },
        "pull_policy": "never",
        "restart": "unless-stopped",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=16m"],
        "ports": [],
        "expose": ["7443"],
        "volumes": [],
        "configs": [],
        "mem_limit": "192m",
        "memswap_limit": "192m",
        "cpus": "0.10",
        "pids_limit": 96,
        "networks": {
            exchange_network: {},
            account_network: {},
            egress_network: {},
        },
        "secrets": [
            {"source": exchange_secret, "target": exchange_secret},
            {"source": provider_secret, "target": provider_secret},
            {"source": "exchange-plugin-root-ca", "target": "exchange-plugin-root-ca"},
            {"source": "exchange-plugin-client-ca", "target": f"{prefix}-client-ca"},
            {"source": server_cert, "target": server_cert},
            {"source": server_key, "target": server_key},
            {"source": health_cert, "target": health_cert},
            {"source": health_key, "target": health_key},
            {"source": account_cert, "target": account_cert},
            {"source": account_key, "target": account_key},
        ],
        "depends_on": {"account-center": {"condition": "service_healthy"}},
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "/usr/local/bin/cyrene-plugin-grpc-health",
                "--target",
                f"{prefix}:7443",
                "--plugin-id",
                provider_id,
                "--server-ca",
                "/run/secrets/exchange-plugin-root-ca",
                "--client-cert",
                f"/run/secrets/{health_cert}",
                "--client-key",
                f"/run/secrets/{health_key}",
            ]
        },
    }


def _provider_configuration(graph: bool) -> dict[str, Any]:
    prefix = "microsoft-graph" if graph else "azure-devops-observation"
    caller = "microsoft-graph-provider" if graph else "azure-devops-observation-provider"
    exchange_ref = f"{prefix}/exchange-verification-key"
    provider_ref = f"{prefix}/account-center-provider-signing-key"
    root_ref = f"{prefix}/account-center-root-certificate"
    cert_ref = f"{prefix}/account-center-client-certificate"
    key_ref = f"{prefix}/account-center-client-key"
    exchange_env = (
        "EXCHANGE_GRAPH_AUTH_CONTEXT_HMAC_KEY" if graph else "EXCHANGE_ADO_AUTH_CONTEXT_HMAC_KEY"
    )
    provider_env = (
        "GRAPH_ACCOUNT_CENTER_PROVIDER_HMAC_KEY"
        if graph
        else "ADO_ACCOUNT_CENTER_PROVIDER_HMAC_KEY"
    )
    root_env = (
        "GRAPH_ACCOUNT_CENTER_ROOT_CERTIFICATE" if graph else "ADO_ACCOUNT_CENTER_ROOT_CERTIFICATE"
    )
    cert_env = (
        "GRAPH_ACCOUNT_CENTER_CLIENT_CERTIFICATE"
        if graph
        else "ADO_ACCOUNT_CENTER_CLIENT_CERTIFICATE"
    )
    key_env = "GRAPH_ACCOUNT_CENTER_CLIENT_KEY" if graph else "ADO_ACCOUNT_CENTER_CLIENT_KEY"
    configuration: dict[str, Any] = {
        "exchange_caller_id": "exchange-service",
        "exchange_verification_key_ref": exchange_ref,
        "account_center": {
            "binding_id": "account-center-profile",
            "connection_ref": "grpcs://account-center:7443",
            "transport": "mtls",
            "caller_id": caller,
            "signing_key_ref": provider_ref,
            "mtls": {
                "root_cert_ref": root_ref,
                "client_cert_ref": cert_ref,
                "client_key_ref": key_ref,
            },
        },
        "hostSecretRefs": {
            exchange_ref: exchange_env,
            provider_ref: provider_env,
            root_ref: root_env,
            cert_ref: cert_env,
            key_ref: key_env,
        },
    }
    if graph:
        configuration.update({"read_only": True, "max_page_size": 50})
    else:
        configuration.update(
            {
                "binding_id": "exchange-azure-devops-observation-binding",
                "organization": "dohorizon",
                "tenant_id": TENANT,
                "authorized_sources": [
                    {
                        "source_id": "pilot-pipelines",
                        "kind": "project_pipelines",
                        "project_id": "99999999-9999-4999-8999-999999999999",
                        "project_name": "Synthetic Project",
                        "profile_ref": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    }
                ],
            }
        )
    return configuration


def _valid_provider_topology() -> dict[str, Any]:
    graph_networks = ["graph-exchange-private", "graph-account-center-private"]
    ado_networks = ["ado-exchange-private", "ado-account-center-private"]
    account_secret_pairs = [
        ("account-center-graph-provider-hmac-key", "account-center-graph-provider-hmac-key"),
        ("account-center-ado-provider-hmac-key", "account-center-ado-provider-hmac-key"),
    ]
    exchange_secret_pairs = [
        ("exchange-graph-auth-context-hmac-key", "exchange-graph-auth-context-hmac-key"),
        ("exchange-ado-auth-context-hmac-key", "exchange-ado-auth-context-hmac-key"),
    ]
    account_map = {
        "CYRENE_ACCOUNT_CENTER_GRAPH_PROVIDER_HMAC_KEY": "/run/secrets/account-center-graph-provider-hmac-key",
        "CYRENE_ACCOUNT_CENTER_ADO_PROVIDER_HMAC_KEY": "/run/secrets/account-center-ado-provider-hmac-key",
    }
    exchange_map = {
        "EXCHANGE_GRAPH_AUTH_CONTEXT_HMAC_KEY": "/run/secrets/exchange-graph-auth-context-hmac-key",
        "EXCHANGE_ADO_AUTH_CONTEXT_HMAC_KEY": "/run/secrets/exchange-ado-auth-context-hmac-key",
    }
    services: dict[str, Any] = {
        "account-center": {
            "environment": {
                "CYRENE_SECRET_FILE_MAP": ",".join(
                    f"{key}={value}" for key, value in account_map.items()
                )
            },
            "secrets": [
                {"source": source, "target": target} for source, target in account_secret_pairs
            ],
            "networks": {name: {} for name in [*graph_networks[1:], ado_networks[1]]},
        },
        "exchange": {
            "environment": {
                "CYRENE_SECRET_FILE_MAP": ",".join(
                    f"{key}={value}" for key, value in exchange_map.items()
                )
            },
            "secrets": [
                {"source": source, "target": target} for source, target in exchange_secret_pairs
            ],
            "networks": {name: {} for name in [graph_networks[0], ado_networks[0]]},
            "depends_on": {
                "microsoft-graph": {"condition": "service_healthy"},
                "azure-devops-observation": {"condition": "service_healthy"},
            },
        },
    }
    for graph, name in ((True, "microsoft-graph"), (False, "azure-devops-observation")):
        services[name] = _provider_service(name, _provider_configuration(graph))
    return {
        "services": services,
        "secrets": {
            name: {"file": f"/private/{name}"}
            for name in (
                "account-center-graph-provider-hmac-key",
                "account-center-ado-provider-hmac-key",
                "exchange-graph-auth-context-hmac-key",
                "exchange-ado-auth-context-hmac-key",
            )
        },
        "networks": {
            "graph-exchange-private": {"internal": True},
            "graph-account-center-private": {"internal": True},
            "graph-egress": {},
            "ado-exchange-private": {"internal": True},
            "ado-account-center-private": {"internal": True},
            "ado-egress": {},
        },
    }


def test_graph_and_ado_preflight_requires_fixed_mtls_and_scoped_host_configuration() -> None:
    config = _valid_provider_topology()

    assert preflight._validate_automation_provider_topology(config)

    changed = copy.deepcopy(config)
    changed["services"]["microsoft-graph"]["ports"] = ["7443:7443"]
    with pytest.raises(preflight.PreflightError, match="fixed bounded mTLS"):
        preflight._validate_automation_provider_topology(changed)

    changed = copy.deepcopy(config)
    changed["services"]["azure-devops-observation"]["healthcheck"]["test"][6] = (
        "cyrene.providers.unsupported"
    )
    with pytest.raises(preflight.PreflightError, match="fixed gRPC mTLS health probe"):
        preflight._validate_automation_provider_topology(changed)


def test_provider_listener_guard_recognizes_only_fixed_graph_and_ado_addresses() -> None:
    service_names = {
        "account-center",
        "github-issues",
        "teams-provider",
        "microsoft-graph",
        "azure-devops-observation",
    }
    config = {
        "services": {name: {"command": ["--listen", "0.0.0.0:7443"]} for name in service_names}
    }

    preflight._validate_provider_listener_addresses(config)
    config["services"]["microsoft-graph"]["command"][1] = "127.0.0.1:7443"
    with pytest.raises(preflight.PreflightError, match="0.0.0.0:7443"):
        preflight._validate_provider_listener_addresses(config)


def test_existing_three_mount_codebuddy_profile_still_passes_without_pilot_overlay(
    tmp_path: Path,
) -> None:
    secrets_dir = tmp_path / "secrets"
    config_dir = tmp_path / "config"
    secrets_dir.mkdir()
    config_dir.mkdir()
    config = {
        "name": "pilot",
        "volumes": {"navigator-sessions": {"name": "pilot_navigator-sessions"}},
        "services": {
            "navigator": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(secrets_dir / "codebuddy"),
                        "target": "/home/navigator/.codebuddy",
                    },
                    {
                        "type": "volume",
                        "source": "navigator-sessions",
                        "target": "/var/lib/cyrene-navigator",
                    },
                    {
                        "type": "bind",
                        "source": str(config_dir),
                        "target": "/run/teams-authorization",
                        "read_only": True,
                    },
                ],
                "secrets": [
                    {"source": name, "target": name}
                    for name in (
                        "navigator-exchange-hmac-key",
                        "navigator-session-token",
                        "teams-navigator-hmac-key",
                        "internal-service-root-ca",
                    )
                ],
                "configs": [
                    {
                        "source": "connector-bindings",
                        "target": "/run/config/connector-bindings.json",
                    },
                    {
                        "source": "connector-tool-catalog",
                        "target": "/run/config/connector-tool-catalog.json",
                    },
                    {
                        "source": "responsibility-targets",
                        "target": "/run/config/responsibility-targets.v1.json",
                    },
                ],
            }
        },
    }

    preflight._validate_codebuddy_acp_mount_boundary(
        config,
        config["services"]["navigator"],
        secrets_dir=secrets_dir,
        config_dir=config_dir,
    )
