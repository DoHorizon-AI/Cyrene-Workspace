"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_preflight.py                                                    │
│  Module: tooling.deploy.teams_cloud.tests.test_preflight                │
│  Role: Exercise deployment preflight gates without touching cloud hosts. │
│                                                                          │
│  模块职责：验证部署预检规则，不触碰云主机。                              │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
preflight = importlib.import_module("preflight")
transfer = importlib.import_module("transfer_arm64")


DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]
UUID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("memory_mib", [3584, 3893])
def test_nominal_four_gib_guest_reservations_do_not_block_host_recognition(
    monkeypatch: pytest.MonkeyPatch, memory_mib: int
) -> None:
    """Accept the reserved-memory guest class in both staging and transfer checks."""

    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(preflight, "_parse_meminfo", lambda: memory_mib)
    monkeypatch.setattr(preflight.os, "cpu_count", lambda: 2)
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        preflight.shutil, "disk_usage", lambda _path: usage(20, 10, 10 * preflight.GIB)
    )
    monkeypatch.setattr(preflight, "_validate_tailscale", lambda: None)

    def remote(_ssh: list[str], argv: list[str], **_kwargs: Any) -> str:
        if argv[0] == "sudo":
            return "aarch64\n"
        if argv[0] == "getconf":
            return "2\n"
        if argv[0] == "awk":
            return str(memory_mib * 1024) + "\n"
        return "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 20971520 10485760 10485760 50% /\n"

    monkeypatch.setattr(transfer, "_remote", remote)
    assert preflight._validate_host(2.0) == (memory_mib, 10.0)
    assert transfer._remote_facts(["ssh"])["memoryMiB"] == memory_mib


def test_host_recognition_still_rejects_smaller_memory_guests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not treat a smaller machine as the approved guest profile."""

    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(preflight, "_parse_meminfo", lambda: 3583)

    def remote(_ssh: list[str], argv: list[str], **_kwargs: Any) -> str:
        if argv[0] == "sudo":
            return "aarch64\n"
        if argv[0] == "getconf":
            return "2\n"
        if argv[0] == "awk":
            return str(3583 * 1024) + "\n"
        return "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 20971520 10485760 10485760 50% /\n"

    monkeypatch.setattr(transfer, "_remote", remote)
    with pytest.raises(preflight.PreflightError, match="guest RAM"):
        preflight._validate_host(2.0)
    with pytest.raises(transfer.TransferError, match="below the pinned profile"):
        transfer._remote_facts(["ssh"])


def _binding(provider: str, operation: str, projection: str, mutating: bool) -> dict[str, Any]:
    """Return one synthetic Exchange binding for a canonical operation."""

    return {
        "bindingId": f"exchange-{provider}-{UUID}",
        "connectionRef": UUID,
        "providerId": provider,
        "profileRef": f"{provider}-primary",
        "tools": [
            {
                "toolId": preflight.CONNECTOR_OPERATION_POLICIES[operation][0],
                "operationId": operation,
                "resourceProjection": projection,
                "mutating": mutating,
            }
        ],
    }


def test_vendor_cli_bindings_require_canonical_mutating_profile_scope(tmp_path: Path) -> None:
    """Allow only the matching provider CLI operation with the profile write grant."""

    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "bindings": [
                    _binding("github", "github.vendor.cli.execute", "profile", True),
                    _binding("huggingface", "huggingface.vendor.cli.execute", "profile", True),
                ],
            }
        ),
        encoding="utf-8",
    )
    preflight._validate_connector_bindings(bindings, {"github", "huggingface"})

    invalid = tmp_path / "invalid-bindings.json"
    invalid.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "bindings": [_binding("github", "github.vendor.cli.execute", "profile", False)],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(preflight.PreflightError, match="not canonical"):
        preflight._validate_connector_bindings(invalid, {"github"})

    wrong_tool_id = tmp_path / "wrong-tool-id.json"
    document = json.loads(bindings.read_text(encoding="utf-8"))
    document["bindings"][0]["tools"][0]["toolId"] = "github.vendor.cli"
    wrong_tool_id.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="not canonical"):
        preflight._validate_connector_bindings(wrong_tool_id, {"github", "huggingface"})


@pytest.mark.parametrize(
    ("operation", "projection", "mutating"),
    [
        ("github.issues.search", "github_repository", False),
        ("github.actions.jobs.list", "github_actions_run", False),
        ("github.actions.job.logs.read", "github_actions_job", False),
        ("github.branch.get", "github_branch", False),
        ("github.file.get", "github_file", False),
        ("github.pull_request.draft.find", "github_pull_request_draft", False),
        ("github.branch.create", "github_branch", True),
        ("github.branch.delete", "github_branch", True),
        ("github.file.commit", "github_file", True),
        ("github.pull_request.draft.create", "github_pull_request_draft", True),
        ("github.actions.rerun_failed", "github_actions_run", True),
    ],
)
def test_proactive_github_operations_use_fixed_target_projections(
    tmp_path: Path, operation: str, projection: str, mutating: bool
) -> None:
    """Pin target identifiers and write classification for proactive GitHub tools."""

    binding = _binding("github", operation, projection, mutating)
    bindings_file = tmp_path / "github-bindings.json"
    bindings_file.write_text(
        json.dumps({"schemaVersion": 1, "bindings": [binding]}), encoding="utf-8"
    )
    preflight._validate_connector_bindings(bindings_file, {"github"})

    binding["tools"][0]["resourceProjection"] = "github_issue"
    bindings_file.write_text(
        json.dumps({"schemaVersion": 1, "bindings": [binding]}), encoding="utf-8"
    )
    with pytest.raises(preflight.PreflightError, match="not canonical"):
        preflight._validate_connector_bindings(bindings_file, {"github"})


@pytest.mark.parametrize(
    ("operation", "projection", "mutating"),
    [
        ("computer.command.execute", "computer_workspace", True),
        ("computer.file.read", "computer_file", False),
        ("computer.file.write", "computer_file", True),
        ("computer.directory.list", "computer_directory", False),
    ],
)
def test_computer_binding_is_limited_to_fixed_workspace_operations(
    tmp_path: Path, operation: str, projection: str, mutating: bool
) -> None:
    """Pin the low-privilege computer tool IDs, workspace projections, and write bits."""

    binding = _binding("computer", operation, projection, mutating)
    bindings_file = tmp_path / "computer-bindings.json"
    bindings_file.write_text(
        json.dumps({"schemaVersion": 1, "bindings": [binding]}), encoding="utf-8"
    )
    preflight._validate_connector_bindings(bindings_file, {"computer"})


def test_computer_runtime_requires_private_runner_and_mtls_proxy(tmp_path: Path) -> None:
    """Reject credential mounts or public networks on the command/file runner."""

    caddyfile = tmp_path / "Caddyfile.computer-runtime"
    caddyfile.write_text(
        "mode require_and_verify\n"
        "trust_pool file /run/secrets/exchange-plugin-client-ca\n"
        "reverse_proxy h2c://computer-runtime:50051\n",
        encoding="utf-8",
    )
    runner_environment = {
        "CYRENE_COMPUTER_WORKSPACE_ROOT": "/workspace",
        "CYRENE_COMPUTER_ARTIFACT_ROOT": "/var/lib/cyrene/artifacts",
        "CYRENE_COMPUTER_COMMAND_DEFAULT_TIMEOUT_MS": "30000",
        "CYRENE_COMPUTER_COMMAND_MAX_TIMEOUT_MS": "600000",
        "CYRENE_COMPUTER_MAX_OUTPUT_BYTES": "65536",
        "CYRENE_COMPUTER_MAX_FILE_BYTES": "1048576",
        "CYRENE_COMPUTER_MAX_DIRECTORY_ENTRIES": "2048",
        "CYRENE_COMPUTER_MAX_DIRECTORY_DEPTH": "32",
        "CYRENE_COMPUTER_MAX_ARTIFACT_BYTES": "16777216",
    }
    computer_profile = {
        "profileRef": "computer-workspace-main",
        "connectionRef": "00000000-0000-4000-8000-000000000004",
        "organizationId": "00000000-0000-4000-8000-000000000001",
        "workspaceId": "00000000-0000-4000-8000-000000000002",
        "expectedIssuer": "https://login.microsoftonline.com/00000000-0000-4000-8000-000000000003/v2.0",
        "directPlugin": {
            "bindingId": "computer-runtime",
            "connectionRef": "grpcs://computer-runtime-proxy:7443",
            "transport": "mtls",
            "mtls": {
                "rootCertificateRef": "tls/plugin-root",
                "clientCertificateRef": "tls/exchange-plugin-client-cert",
                "clientKeyRef": "tls/exchange-plugin-client-key",
            },
        },
        "maxCommandTimeoutMs": 600000,
        "maxFileBytes": 1048576,
        "maxReadBytes": 65536,
        "maxDirectoryDepth": 32,
    }
    host_secret_refs = {
        "tls/plugin-root": "EXCHANGE_COMPUTER_PROXY_ROOT_CA_FILE",
        "tls/exchange-plugin-client-cert": "EXCHANGE_COMPUTER_PROXY_CLIENT_CERT_FILE",
        "tls/exchange-plugin-client-key": "EXCHANGE_COMPUTER_PROXY_CLIENT_KEY_FILE",
    }
    config = {
        "services": {
            "navigator": {
                "environment": {"TEAMS_TENANT_ID": "00000000-0000-4000-8000-000000000003"}
            },
            "workspace-authority": {
                "environment": {
                    "CYRENE_ORGANIZATION_ID": "00000000-0000-4000-8000-000000000001",
                    "CYRENE_WORKSPACE_ID": "00000000-0000-4000-8000-000000000002",
                }
            },
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                        {
                            "hostSecretRefs": host_secret_refs,
                            "connectorHost": {"computerRunner": computer_profile},
                        }
                    ),
                    "EXCHANGE_COMPUTER_PROXY_ROOT_CA_FILE": "/run/secrets/exchange-plugin-root-ca",
                    "EXCHANGE_COMPUTER_PROXY_CLIENT_CERT_FILE": "/run/secrets/exchange-plugin-client-cert",
                    "EXCHANGE_COMPUTER_PROXY_CLIENT_KEY_FILE": "/run/secrets/exchange-plugin-client-key",
                },
                "secrets": [
                    {"target": "exchange-plugin-root-ca"},
                    {"target": "exchange-plugin-client-cert"},
                    {"target": "exchange-plugin-client-key"},
                ],
                "networks": {"exchange-computer-private": {}},
            },
            "computer-runtime": {
                "environment": runner_environment,
                "user": "10001:10001",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "tmpfs": [
                    "/tmp:rw,nosuid,nodev,size=64m",
                    "/workspace:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=192m",
                    "/var/lib/cyrene/artifacts:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=64m",
                ],
                "networks": {"computer-runtime-private": {}},
                "volumes": [],
                "mem_limit": "512m",
                "cpus": "0.10",
            },
            "computer-runtime-proxy": {
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "mem_limit": "64m",
                "cpus": "0.05",
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(caddyfile),
                        "target": "/etc/caddy/Caddyfile",
                        "read_only": True,
                    }
                ],
                "secrets": [
                    {"source": "computer-runtime-proxy-server-cert"},
                    {"source": "computer-runtime-proxy-server-key"},
                    {"source": "exchange-plugin-client-ca"},
                ],
                "networks": {
                    "exchange-computer-private": {},
                    "computer-runtime-private": {},
                },
            },
        },
        "volumes": {},
        "networks": {
            "computer-runtime-private": {"internal": True},
            "exchange-computer-private": {"internal": True},
        },
    }
    preflight._validate_computer_runtime_boundary(config)

    config["services"]["computer-runtime"]["tmpfs"][1] = (
        "/workspace:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=512m"
    )
    with pytest.raises(preflight.PreflightError, match="bounded scratch tmpfs"):
        preflight._validate_computer_runtime_boundary(config)
    config["services"]["computer-runtime"]["tmpfs"][1] = (
        "/workspace:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=192m"
    )

    exchange_environment = config["services"]["exchange"]["environment"]
    integration = json.loads(exchange_environment["CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"])
    integration["connectorHost"]["computerRunner"]["maxReadBytes"] = 1048576
    exchange_environment["CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"] = json.dumps(integration)
    with pytest.raises(preflight.PreflightError, match="operation limits"):
        preflight._validate_computer_runtime_boundary(config)

    integration["connectorHost"]["computerRunner"] = computer_profile
    exchange_environment["CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"] = json.dumps(integration)
    config["services"]["computer-runtime"]["secrets"] = ["github-app-private-key"]
    with pytest.raises(preflight.PreflightError, match="no credentials"):
        preflight._validate_computer_runtime_boundary(config)


def test_computer_runtime_scratch_is_ephemeral_and_navigator_state_stays_persistent() -> None:
    """Pin the runner tmpfs budget while preserving the base Navigator state volume."""

    base_compose = yaml.safe_load((DEPLOYMENT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    runner_compose = yaml.safe_load(
        (DEPLOYMENT_ROOT / "compose.computer-runtime.yaml").read_text(encoding="utf-8")
    )
    runner = runner_compose["services"]["computer-runtime"]
    assert set(runner["tmpfs"]) == {
        "/tmp:rw,nosuid,nodev,size=64m",
        "/workspace:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=192m",
        "/var/lib/cyrene/artifacts:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=64m",
    }
    assert sum(int(row.rsplit("size=", 1)[1].removesuffix("m")) for row in runner["tmpfs"]) == 320
    assert runner.get("volumes", []) == []

    persistence_volumes = base_compose["services"]["navigator-persistence"]["volumes"]
    assert "navigator-sessions:/var/lib/cyrene-navigator" in persistence_volumes


def test_empty_connector_bindings_are_bootstrap_only(tmp_path: Path) -> None:
    """Allow an empty map only while the non-ready dependency bootstrap is being prepared."""

    bindings = tmp_path / "bindings.json"
    bindings.write_text('{"schemaVersion":1,"bindings":[]}', encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="at least one Exchange-issued"):
        preflight._validate_connector_bindings(bindings, {"github", "huggingface"})
    preflight._validate_connector_bindings(bindings, {"github", "huggingface"}, allow_empty=True)


def test_compose_configs_must_be_readable_by_runtime_uid_10001() -> None:
    """Reject operator-owned files that mounted service users cannot read."""

    preflight._validate_runtime_config_metadata(10001, 0o100600, "connector-bindings.json")
    with pytest.raises(preflight.PreflightError, match="owned by runtime UID 10001"):
        preflight._validate_runtime_config_metadata(1000, 0o100600, "connector-bindings.json")
    with pytest.raises(preflight.PreflightError, match="owner-readable"):
        preflight._validate_runtime_config_metadata(10001, 0o100000, "connector-bindings.json")


def test_proactive_model_profile_has_gemini_and_no_paid_fallback() -> None:
    """Require the real default model and a bounded backup while chat has priority."""

    environment = {
        "CYRENE_HARNESS_MODEL_SOURCE": "antigravity",
        "CYRENE_HARNESS_MODEL": "gemini-3.8-flash-medium",
        "CYRENE_HARNESS_CODEBUDDY_FALLBACK_ENABLED": "true",
        "CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP": "true",
        "CYRENE_HARNESS_CODEBUDDY_FALLBACK_MODEL": "glm-5.3-flash",
        "CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS": "2",
    }
    preflight._validate_model_runtime_config(environment)

    environment["CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP"] = "false"
    with pytest.raises(preflight.PreflightError, match="no-additional-spend"):
        preflight._validate_model_runtime_config(environment)


def test_legacy_dsh_codebuddy_model_profile_remains_compatible() -> None:
    """Keep the existing DSH-to-CodeBuddy model adapter as the default backend."""

    preflight._validate_model_runtime_config(
        {
            "CYRENE_HARNESS_MODEL_SOURCE": "codebuddy",
            "CYRENE_HARNESS_MODEL": "glm-5.3-flash",
            "CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS": "1",
        }
    )


def test_codebuddy_acp_model_profile_is_native_and_closed() -> None:
    """Accept only the fixed native CLI, approved models, and explicit no-top-up policy."""

    environment = {
        "CYRENE_HARNESS_EXECUTION_BACKEND": "codebuddy-acp",
        "CYRENE_HARNESS_CODEBUDDY_ACP_COMMAND": "codebuddy",
        "CYRENE_HARNESS_CODEBUDDY_ACP_MODEL": "glm-5.3-flash",
        "CYRENE_HARNESS_MODEL_SOURCE": "",
        "CYRENE_HARNESS_MODEL": "",
        "CYRENE_HARNESS_CODEBUDDY_FALLBACK_ENABLED": "false",
        "CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP": "true",
        "CYRENE_HARNESS_CODEBUDDY_FALLBACK_MODEL": "",
        "CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS": "2",
    }
    preflight._validate_model_runtime_config(environment)

    environment["CYRENE_HARNESS_CODEBUDDY_ACP_MODEL"] = "deepseek-v4.1-flash"
    preflight._validate_model_runtime_config(environment)

    invalid_profiles = [
        ("CYRENE_HARNESS_CODEBUDDY_ACP_COMMAND", "codebuddy --acp", "command must be exactly"),
        ("CYRENE_HARNESS_CODEBUDDY_ACP_COMMAND", "/usr/bin/codebuddy", "command must be exactly"),
        (
            "CYRENE_HARNESS_CODEBUDDY_ACP_MODEL",
            "gemini-3.8-flash-medium",
            "approved GLM or DeepSeek",
        ),
        ("CYRENE_HARNESS_MODEL_SOURCE", "codebuddy", "legacy Harness model source"),
        ("CYRENE_HARNESS_MODEL_SOURCE", "antigravity", "legacy Harness model source"),
        ("CYRENE_HARNESS_MODEL", "glm-5.3-flash", "legacy Harness model source"),
        ("CYRENE_HARNESS_CODEBUDDY_FALLBACK_ENABLED", "true", "disable model fallback"),
        ("CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP", "false", "no-paid-topup declaration"),
        ("CYRENE_HARNESS_CODEBUDDY_FALLBACK_MODEL", "deepseek-v4.1-flash", "legacy fallback model"),
        (
            "CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS",
            "1",
            "two bounded foreground/background task slots",
        ),
        (
            "CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS",
            "3",
            "two bounded foreground/background task slots",
        ),
    ]
    for key, value, error in invalid_profiles:
        invalid = dict(environment)
        invalid[key] = value
        with pytest.raises(preflight.PreflightError, match=error):
            preflight._validate_model_runtime_config(invalid)

    with pytest.raises(preflight.PreflightError, match="backend must be dsh or codebuddy-acp"):
        preflight._validate_model_runtime_config(
            {"CYRENE_HARNESS_EXECUTION_BACKEND": "antigravity"}
        )


def test_codebuddy_acp_compose_profile_is_opt_in_and_keeps_runtime_boundaries() -> None:
    """Keep ACP selection explicit, credentials host-owned, and existing service budgets intact."""

    base = yaml.safe_load((DEPLOYMENT_ROOT / "compose.yaml").read_text())
    profile = yaml.safe_load((DEPLOYMENT_ROOT / "compose.codebuddy-acp.yaml").read_text())
    assert set(profile["services"]) == {"navigator"}
    navigator = profile["services"]["navigator"]
    environment = navigator["environment"]
    base_navigator = base["services"]["navigator"]
    merged_environment = {
        **base_navigator["environment"],
        **environment,
    }
    assert merged_environment["CYRENE_HARNESS_EXECUTION_BACKEND"] == "codebuddy-acp"
    assert merged_environment["CYRENE_HARNESS_MODEL_SOURCE"] == ""
    assert merged_environment["CYRENE_HARNESS_MODEL"] == ""
    assert environment["CYRENE_HARNESS_CODEBUDDY_ACP_COMMAND"] == (
        "${CYRENE_HARNESS_CODEBUDDY_ACP_COMMAND:-codebuddy}"
    )
    assert environment["CYRENE_HARNESS_CODEBUDDY_ACP_MODEL"] == (
        "${CYRENE_HARNESS_CODEBUDDY_ACP_MODEL:-glm-5.3-flash}"
    )
    assert environment["CYRENE_HARNESS_CODEBUDDY_FALLBACK_ENABLED"] == "false"
    assert environment["CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP"] == "true"
    assert environment["CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS"] == "2"
    assert environment["CYRENE_WORKFLOWS_ENABLED"] == "${CYRENE_WORKFLOWS_ENABLED:-false}"
    assert not any("MCP" in name.upper() for name in environment)
    assert navigator["read_only"] is True
    assert navigator["cap_drop"] == ["ALL"]
    assert navigator["security_opt"] == ["no-new-privileges:true"]
    assert navigator["tmpfs"] == ["/tmp:rw,nosuid,nodev,size=128m"]
    for key in (
        "image",
        "command",
        "entrypoint",
        "ports",
        "privileged",
        "cap_add",
        "mem_limit",
        "cpus",
        "secrets",
        "volumes",
    ):
        assert key not in navigator
    assert profile["configs"]["responsibility-targets"]["file"].startswith(
        "${CYRENE_RESPONSIBILITY_TARGETS_HOST_FILE:"
    )
    assert base_navigator["mem_limit"] == "768m"
    assert base_navigator["cpus"] == "0.65"

    workflow = json.loads(
        (DEPLOYMENT_ROOT / "config" / "proactive-assistant-workflow.v2.template.json").read_text()
    )
    assert workflow["enabled"] is False
    preflight._validate_workflow_definition_disabled(workflow)
    assert (
        "CYRENE_HF_ENABLED=false"
        in (DEPLOYMENT_ROOT / "config" / "codebuddy-acp.env.template").read_text()
    )
    assert (
        "CYRENE_WORKFLOWS_ENABLED=false"
        in (DEPLOYMENT_ROOT / "config" / "codebuddy-acp.env.template").read_text()
    )


def test_github_only_compose_has_no_hf_service_seed_or_provider_secrets() -> None:
    """Keep Hf provider, bootstrap seed, and Hf-only credentials in an explicit overlay."""

    base = yaml.safe_load((DEPLOYMENT_ROOT / "compose.yaml").read_text())
    huggingface = yaml.safe_load((DEPLOYMENT_ROOT / "compose.huggingface.yaml").read_text())
    optional_secrets = set(preflight.HUGGINGFACE_OPTIONAL_SECRET_NAMES)
    assert "huggingface" not in base["services"]
    assert "huggingface" in huggingface["services"]
    assert (
        base["services"]["account-center"]["labels"]["com.cyrene.huggingface-enabled"]
        == "${CYRENE_HF_ENABLED:-true}"
    )
    assert not optional_secrets & set(base["secrets"])
    assert set(huggingface["secrets"]) == optional_secrets
    assert "huggingface-oauth.json" not in {
        row.get("target")
        for row in base["services"]["account-center"].get("secrets", [])
        if isinstance(row, dict)
    }
    assert not any("huggingface" in name for name in base["services"]["exchange"]["depends_on"])

    github_only_bootstrap = json.loads(
        (
            DEPLOYMENT_ROOT / "config" / "private-connector-bootstrap.github-only.template.json"
        ).read_text()
    )
    assert set(github_only_bootstrap) == {"schemaVersion", "actor", "github"}
    assert (
        "CYRENE_HF_ENABLED=false"
        in (DEPLOYMENT_ROOT / "config" / "github-only.env.template").read_text()
    )


def test_workflows_engine_does_not_enable_a_definition() -> None:
    """Keep scheduled execution disabled until the real owner and connector gates pass."""

    preflight._validate_workflow_definition_disabled({"enabled": False})
    with pytest.raises(
        preflight.PreflightError, match="must remain disabled until real acceptance"
    ):
        preflight._validate_workflow_definition_disabled({"enabled": True})


@pytest.mark.parametrize("model", ["glm-5.3-flash", "deepseek-v4.1-flash"])
def test_codebuddy_acp_workflow_metadata_matches_selected_native_model(model: str) -> None:
    """Keep ACP WorkFlow metadata aligned with its runtime model and no-fallback policy."""

    compose = {
        "services": {
            "navigator": {
                "environment": {
                    "CYRENE_HARNESS_EXECUTION_BACKEND": "codebuddy-acp",
                    "CYRENE_HARNESS_CODEBUDDY_ACP_MODEL": model,
                }
            }
        }
    }
    workflow = {
        "modelPolicy": {
            "preferredProvider": "codebuddy",
            "preferredModel": model,
            "fallback": "configured_no_additional_spend",
        }
    }
    preflight._validate_workflow_model_policy(compose, workflow)

    workflow["modelPolicy"]["preferredProvider"] = "antigravity"
    with pytest.raises(preflight.PreflightError, match="native model and no-spend metadata"):
        preflight._validate_workflow_model_policy(compose, workflow)


def test_codebuddy_acp_rejects_compose_owned_mcp_credentials() -> None:
    """Keep the per-run host bridge token out of environment, mounts, and CLI overrides."""

    valid = yaml.safe_load((DEPLOYMENT_ROOT / "compose.codebuddy-acp.yaml").read_text())
    secrets_dir = Path("/private/secrets")
    config_dir = Path("/private/config")
    valid["name"] = "cyrene-teams-cloud"
    valid["volumes"] = {"navigator-sessions": {"name": "cyrene-teams-cloud_navigator-sessions"}}
    navigator = valid["services"]["navigator"]
    navigator["environment"]["CYRENE_WORKFLOWS_ENABLED"] = "false"
    navigator["volumes"] = [
        {
            "type": "bind",
            "source": str(secrets_dir / "codebuddy"),
            "target": "/home/navigator/.codebuddy",
            "read_only": False,
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
    ]
    navigator["secrets"] = [
        {"source": name, "target": name}
        for name in (
            "navigator-exchange-hmac-key",
            "navigator-session-token",
            "teams-navigator-hmac-key",
            "internal-service-root-ca",
        )
    ]
    navigator["configs"] = [
        {"source": "connector-bindings", "target": "/run/config/connector-bindings.json"},
        {
            "source": "connector-tool-catalog",
            "target": "/run/config/connector-tool-catalog.json",
        },
        {
            "source": "responsibility-targets",
            "target": "/run/config/responsibility-targets.v1.json",
        },
    ]
    preflight._validate_codebuddy_acp_boundary(
        valid, secrets_dir=secrets_dir, config_dir=config_dir
    )

    enabled_workflows = json.loads(json.dumps(valid))
    enabled_workflows["services"]["navigator"]["environment"]["CYRENE_WORKFLOWS_ENABLED"] = "true"
    preflight._validate_codebuddy_acp_boundary(
        enabled_workflows, secrets_dir=secrets_dir, config_dir=config_dir
    )
    enabled_workflows["services"]["navigator"]["environment"]["CYRENE_WORKFLOWS_ENABLED"] = "yes"
    with pytest.raises(preflight.PreflightError, match="explicitly false or true"):
        preflight._validate_codebuddy_acp_boundary(
            enabled_workflows, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_mcp_environment = json.loads(json.dumps(valid))
    with_mcp_environment["services"]["navigator"]["environment"][
        "CYRENE_HARNESS_CODEBUDDY_ACP_MCP_CONFIG"
    ] = "/run/config/mcp.json"
    with pytest.raises(preflight.PreflightError, match="not be injected through Compose env"):
        preflight._validate_codebuddy_acp_boundary(
            with_mcp_environment, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_mcp_mount = json.loads(json.dumps(valid))
    with_mcp_mount["services"]["navigator"]["volumes"] = [
        {"type": "bind", "source": "/private/mcp.json", "target": "/run/mcp.json"}
    ]
    with pytest.raises(preflight.PreflightError, match="stay out of Compose mounts and secrets"):
        preflight._validate_codebuddy_acp_boundary(
            with_mcp_mount, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_legacy_home = json.loads(json.dumps(valid))
    with_legacy_home["services"]["navigator"]["volumes"] = [
        {"type": "bind", "source": "/private/model-home", "target": "/home/navigator"}
    ]
    with pytest.raises(preflight.PreflightError, match="legacy Antigravity model home"):
        preflight._validate_codebuddy_acp_boundary(
            with_legacy_home, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_extra_host_bind = json.loads(json.dumps(valid))
    with_extra_host_bind["services"]["navigator"]["volumes"].append(
        {
            "type": "bind",
            "source": "/host/private",
            "target": "/run/extra",
            "read_only": True,
        }
    )
    with pytest.raises(preflight.PreflightError, match="only its approved login, session"):
        preflight._validate_codebuddy_acp_boundary(
            with_extra_host_bind, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_foreign_actor_source = json.loads(json.dumps(valid))
    actor_mount = next(
        row
        for row in with_foreign_actor_source["services"]["navigator"]["volumes"]
        if row.get("target") == "/run/teams-authorization"
    )
    actor_mount["source"] = "/host/other-config"
    with pytest.raises(preflight.PreflightError, match="approved login, session"):
        preflight._validate_codebuddy_acp_boundary(
            with_foreign_actor_source, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_foreign_codebuddy_source = json.loads(json.dumps(valid))
    codebuddy_mount = next(
        row
        for row in with_foreign_codebuddy_source["services"]["navigator"]["volumes"]
        if row.get("target") == "/home/navigator/.codebuddy"
    )
    codebuddy_mount["source"] = "/host/other-codebuddy-state"
    with pytest.raises(preflight.PreflightError, match="approved login, session"):
        preflight._validate_codebuddy_acp_boundary(
            with_foreign_codebuddy_source, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_foreign_session_volume = json.loads(json.dumps(valid))
    session_mount = next(
        row
        for row in with_foreign_session_volume["services"]["navigator"]["volumes"]
        if row.get("target") == "/var/lib/cyrene-navigator"
    )
    session_mount["source"] = "other-session-volume"
    with pytest.raises(preflight.PreflightError, match="approved login, session"):
        preflight._validate_codebuddy_acp_boundary(
            with_foreign_session_volume, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_custom_command = json.loads(json.dumps(valid))
    with_custom_command["services"]["navigator"]["command"] = ["sh", "-c", "codebuddy"]
    with pytest.raises(preflight.PreflightError, match="Navigator-owned fixed CLI invocation"):
        preflight._validate_codebuddy_acp_boundary(
            with_custom_command, secrets_dir=secrets_dir, config_dir=config_dir
        )

    with_unbounded_temp = json.loads(json.dumps(valid))
    with_unbounded_temp["services"]["navigator"]["tmpfs"] = ["/tmp:rw,nosuid,nodev,size=1g"]
    with pytest.raises(preflight.PreflightError, match="bounded 128 MiB temporary storage"):
        preflight._validate_codebuddy_acp_boundary(
            with_unbounded_temp, secrets_dir=secrets_dir, config_dir=config_dir
        )


def test_proactive_navigator_hardening_requires_read_only_root_and_dropped_caps() -> None:
    """Keep model credentials out of the writable runtime root and drop all Linux capabilities."""

    config = {
        "services": {
            "navigator": {
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "tmpfs": ["/tmp:rw,nosuid,nodev,size=128m"],
                "environment": {
                    "CYRENE_RESPONSIBILITY_TARGETS_FILE": "/run/config/responsibility-targets.v1.json"
                },
                "configs": [
                    {
                        "source": "responsibility-targets",
                        "target": "/run/config/responsibility-targets.v1.json",
                    }
                ],
            }
        }
    }
    preflight._validate_proactive_navigator_container(config)
    config["services"]["navigator"]["read_only"] = False
    with pytest.raises(preflight.PreflightError, match="read-only root"):
        preflight._validate_proactive_navigator_container(config)


def test_responsibility_targets_require_current_binding_and_exact_safe_grants(
    tmp_path: Path,
) -> None:
    """Reject unbound targets or grants that exceed the reviewed draft/CI operation set."""

    binding_id = f"exchange-github-{UUID}"
    bindings_path = tmp_path / "bindings.json"
    bindings_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "bindings": [_binding("github", "github.branch.get", "github_branch", False)],
            }
        ),
        encoding="utf-8",
    )
    template_path = DEPLOYMENT_ROOT / "config" / "responsibility-targets.v1.template.json"
    target = json.loads(template_path.read_text(encoding="utf-8"))["targets"][0]
    target.update(
        {
            "id": "123456789",
            "label": "DoHorizon-AI/Cyrene-Test",
            "bindingId": binding_id,
            "uri": "https://github.com/DoHorizon-AI/Cyrene-Test",
        }
    )
    next(grant for grant in target["actionGrants"] if grant["id"] == "rerun_source_ci")[
        "sourceWorkflowIds"
    ] = ["1234567890"]
    config_path = tmp_path / "responsibility-targets.v1.json"
    config_path.write_text(json.dumps({"schemaVersion": 1, "targets": [target]}), encoding="utf-8")
    preflight._validate_responsibility_targets(config_path, bindings_path)

    target["bindingId"] = "UNBOUND_PENDING_EXCHANGE_GITHUB_REBIND"
    config_path.write_text(json.dumps({"schemaVersion": 1, "targets": [target]}), encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="current Exchange GitHub binding"):
        preflight._validate_responsibility_targets(config_path, bindings_path)

    target["bindingId"] = binding_id
    target["actionGrants"].append(
        {
            "id": "update_issue_progress",
            "action": "update_issue_progress",
            "baseBranches": [],
            "branchPrefixes": [],
            "pathPrefixes": [],
            "issueNumbers": [7],
            "permittedOperations": ["github.issue.comment.create"],
        }
    )
    config_path.write_text(json.dumps({"schemaVersion": 1, "targets": [target]}), encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="contain only the approved actions"):
        preflight._validate_responsibility_targets(config_path, bindings_path)


def test_bootstrap_allows_only_the_named_pending_github_binding_and_workflow_id(
    tmp_path: Path,
) -> None:
    """Allow a closed pending target skeleton before Exchange exports real bindings."""

    template_path = DEPLOYMENT_ROOT / "config" / "responsibility-targets.v1.template.json"
    target = json.loads(template_path.read_text(encoding="utf-8"))["targets"][0]
    target.update(
        {
            "id": "123456789",
            "label": "DoHorizon-AI/Cyrene-Test",
            "bindingId": "UNBOUND_PENDING_EXCHANGE_GITHUB_REBIND",
            "uri": "https://github.com/DoHorizon-AI/Cyrene-Test",
        }
    )
    targets_path = tmp_path / "responsibility-targets.v1.json"
    bindings_path = tmp_path / "connector-bindings.json"
    targets_path.write_text(json.dumps({"schemaVersion": 1, "targets": [target]}), encoding="utf-8")
    bindings_path.write_text('{"schemaVersion":1,"bindings":[]}', encoding="utf-8")
    preflight._validate_responsibility_targets(targets_path, bindings_path, allow_pending=True)

    with pytest.raises(preflight.PreflightError, match="current Exchange GitHub binding"):
        preflight._validate_responsibility_targets(targets_path, bindings_path)

    target["bindingId"] = "UNBOUND_NOT_APPROVED"
    targets_path.write_text(json.dumps({"schemaVersion": 1, "targets": [target]}), encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="current Exchange GitHub binding"):
        preflight._validate_responsibility_targets(targets_path, bindings_path, allow_pending=True)


def test_bootstrap_accepts_missing_owner_thread_ids_only_while_workflow_is_disabled(
    tmp_path: Path,
) -> None:
    """Keep first-chat thread/task IDs pending until a real owner conversation exists."""

    artifact_root = tmp_path / ".artifacts" / "teams-cloud-test"
    config_dir = artifact_root / "config"
    config_dir.mkdir(parents=True)
    preflight._validate_optional_workflow_definition_disabled(config_dir)

    workflow_path = artifact_root / "proactive-assistant-workflow.v2.json"
    workflow_path.write_text(
        json.dumps(
            {
                "enabled": False,
                "authorization": {
                    "threadBindingId": "UNBOUND_PENDING_OWNER_THREAD",
                    "ownerTaskId": "UNBOUND_PENDING_OWNER_TASK",
                },
            }
        ),
        encoding="utf-8",
    )
    workflow_path.chmod(0o600)
    preflight._validate_optional_workflow_definition_disabled(config_dir)

    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["enabled"] = True
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    with pytest.raises(
        preflight.PreflightError, match="must remain disabled until real acceptance"
    ):
        preflight._validate_optional_workflow_definition_disabled(config_dir)


@pytest.mark.parametrize("computer_runner_enabled", [False, True])
def test_bootstrap_preflight_is_explicitly_not_full_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, computer_runner_enabled: bool
) -> None:
    """Return bootstrap status and the configured computer scratch lifetime."""

    artifact_root = tmp_path / ".artifacts" / "teams-cloud-test"
    config_dir = artifact_root / "config"
    secrets_dir = artifact_root / "secrets"
    config_dir.mkdir(parents=True)
    secrets_dir.mkdir()
    environment_file = artifact_root / "runtime.env"
    environment_file.write_text("TEST=1\n", encoding="utf-8")
    environment_file.chmod(0o600)
    image_lock = artifact_root / "image-lock.json"
    image_lock.write_text(
        json.dumps({"images": [{"image": "locked:test"}], "externalImages": {}}),
        encoding="utf-8",
    )
    image_lock.chmod(0o600)

    monkeypatch.setattr(preflight, "_validate_host", lambda _required: (4096, 10.0))
    monkeypatch.setattr(preflight, "check_local_lock", lambda *_args: None)
    services = {"exchange": {"image": "locked:test"}}
    if computer_runner_enabled:
        services["computer-runtime"] = {}
    monkeypatch.setattr(preflight, "_compose_config", lambda *_args: {"services": services})
    monkeypatch.setattr(preflight, "_validate_compose", lambda *_args: (100.0, 0.2))
    monkeypatch.setattr(preflight, "_parse_memavailable", lambda: 2048)
    monkeypatch.setattr(
        preflight,
        "_memory_headroom",
        lambda **_kwargs: {
            "status": "READY",
            "memAvailableMiB": 2048,
            "minimumMemAvailableMiB": 1024,
            "additionalPhaseMemoryMiB": 0,
            "runtimeRssMeasurementRequired": False,
        },
    )
    for name in (
        "_validate_internal_https_topology",
        "_validate_profile_stage",
        "_validate_secrets_and_configs",
        "_validate_identity_scope",
        "_validate_stage_acceptance_receipts",
        "_validate_host_ports",
    ):
        monkeypatch.setattr(preflight, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(preflight, "_validate_cutover_runtime", lambda *_args: {})

    report = preflight.run_preflight(
        mode="bootstrap",
        compose_files=[tmp_path / "compose.yaml"],
        env_files=[environment_file],
        image_lock=image_lock,
        secrets_dir=secrets_dir,
        config_dir=config_dir,
        github_hf_evidence=None,
        azure_gcp_ledger=None,
        runpod_ledger=None,
        azure_gcp_api_evidence=None,
        runpod_api_evidence=None,
        docker_prefix=["docker"],
        max_memory_mib=1024,
        max_cpus=1.0,
        required_free_gib=1.0,
        minimum_mem_available_mib=1024,
    )
    assert report["status"] == "BOOTSTRAP_ONLY_NOT_FULL_READY"
    assert report["deploymentReady"] is False
    if computer_runner_enabled:
        assert report["computerRuntimeStorage"] == {
            "lifetime": "EPHEMERAL",
            "workspaceClearedOnRestart": True,
            "artifactRefsSurviveRestart": False,
        }
    else:
        assert report["computerRuntimeStorage"] == {
            "lifetime": "NOT_PRESENT",
            "workspaceClearedOnRestart": None,
            "artifactRefsSurviveRestart": None,
        }


def test_exchange_tool_catalog_must_match_exact_binding_and_hide_credentials(
    tmp_path: Path,
) -> None:
    """Require live provider schemas to match the pinned connection and operation map."""

    binding = _binding("github", "github.repository.list", "empty", False)
    bindings_file = tmp_path / "connector-bindings.json"
    bindings_file.write_text(json.dumps({"schemaVersion": 1, "bindings": [binding]}))
    catalog = {
        "schemaVersion": 1,
        "items": [
            {
                "bindingId": binding["bindingId"],
                "capabilityId": "tool.provider.v1",
                "providerId": "github",
                "connectionRef": UUID,
                "profileRef": "github-primary",
                "tools": [
                    {
                        "id": binding["tools"][0]["toolId"],
                        "name": "List repositories",
                        "description": "List repositories from the linked profile.",
                        "readOnly": True,
                        "inputSchema": {
                            "type": "object",
                            "properties": {"profile_ref": {"type": "string"}},
                            "required": ["profile_ref"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        ],
    }
    catalog_file = tmp_path / "connector-tool-catalog.json"
    catalog_file.write_text(json.dumps(catalog))
    preflight._validate_connector_tool_catalog(
        catalog_file, bindings_file, {"github", "huggingface"}
    )

    wrong_access = json.loads(json.dumps(catalog))
    wrong_access["items"][0]["tools"][0]["readOnly"] = False
    catalog_file.write_text(json.dumps(wrong_access))
    with pytest.raises(preflight.PreflightError, match="conflicts with the fixed operation map"):
        preflight._validate_connector_tool_catalog(
            catalog_file, bindings_file, {"github", "huggingface"}
        )

    with_secret = json.loads(json.dumps(catalog))
    with_secret["items"][0]["tools"][0]["inputSchema"]["properties"]["api_token"] = {
        "type": "string"
    }
    catalog_file.write_text(json.dumps(with_secret))
    with pytest.raises(preflight.PreflightError, match="must not accept credential fields"):
        preflight._validate_connector_tool_catalog(
            catalog_file, bindings_file, {"github", "huggingface"}
        )


@pytest.mark.parametrize(
    ("operation", "target_fields"),
    [
        ("github.issues.search", ("repository_id", "repository")),
        ("github.actions.jobs.list", ("repository_id", "repository", "run_id")),
        (
            "github.actions.job.logs.read",
            ("repository_id", "repository", "run_id", "job_id"),
        ),
        ("github.branch.get", ("repository_id", "repository", "branch")),
        ("github.branch.create", ("repository_id", "repository", "branch")),
        ("github.branch.delete", ("repository_id", "repository", "branch")),
        ("github.file.get", ("repository_id", "repository", "branch", "path")),
        ("github.file.commit", ("repository_id", "repository", "branch", "path")),
        (
            "github.pull_request.draft.find",
            ("repository_id", "repository", "head_branch", "base_branch"),
        ),
        (
            "github.pull_request.draft.create",
            ("repository_id", "repository", "head_branch", "base_branch"),
        ),
        (
            "github.actions.rerun_failed",
            (
                "repository_id",
                "repository",
                "run_id",
                "expected_workflow_id",
                "expected_head_branch",
                "expected_head_sha",
                "expected_attempt",
            ),
        ),
        ("computer.command.execute", ("workspace_ref",)),
        ("computer.file.read", ("workspace_ref", "path", "length")),
        ("computer.file.write", ("workspace_ref", "path")),
        ("computer.directory.list", ("workspace_ref", "path")),
    ],
)
def test_connector_catalog_requires_exact_target_locator_fields(
    tmp_path: Path, operation: str, target_fields: tuple[str, ...]
) -> None:
    """Require every projected locator and rerun receipt field in the exported schema."""

    binding = _binding(
        "computer" if operation.startswith("computer.") else "github",
        operation,
        preflight.CONNECTOR_OPERATION_POLICIES[operation][1],
        preflight.CONNECTOR_OPERATION_POLICIES[operation][2],
    )
    input_schema = {
        "type": "object",
        "properties": {field: {"type": "string"} for field in target_fields},
        "required": list(target_fields),
        "additionalProperties": False,
    }
    if operation == "github.actions.rerun_failed":
        input_schema["properties"].update(
            {
                "expected_workflow_id": {
                    "type": "string",
                    "pattern": r"^[1-9][0-9]{0,19}$",
                },
                "expected_head_branch": {"type": "string", "maxLength": 255},
                "expected_head_sha": {
                    "type": "string",
                    "pattern": r"^[0-9a-fA-F]{40}$",
                },
                "expected_attempt": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2_147_483_647,
                },
            }
        )
    tool = binding["tools"][0]
    catalog_item = {
        "bindingId": binding["bindingId"],
        "capabilityId": "tool.provider.v1",
        "providerId": binding["providerId"],
        "connectionRef": UUID,
        "profileRef": binding["profileRef"],
        "tools": [
            {
                "id": tool["toolId"],
                "name": "Typed GitHub operation",
                "description": "A fixed operation scoped to one target.",
                "readOnly": not tool["mutating"],
                "inputSchema": input_schema,
            }
        ],
    }
    bindings_file = tmp_path / "github-bindings.json"
    catalog_file = tmp_path / "github-catalog.json"
    bindings_file.write_text(
        json.dumps({"schemaVersion": 1, "bindings": [binding]}), encoding="utf-8"
    )
    catalog_file.write_text(
        json.dumps({"schemaVersion": 1, "items": [catalog_item]}), encoding="utf-8"
    )
    preflight._validate_connector_tool_catalog(catalog_file, bindings_file, {binding["providerId"]})

    input_schema["required"] = list(target_fields[:-1])
    catalog_file.write_text(
        json.dumps({"schemaVersion": 1, "items": [catalog_item]}), encoding="utf-8"
    )
    with pytest.raises(
        preflight.PreflightError, match="require every canonical target and evidence"
    ):
        preflight._validate_connector_tool_catalog(
            catalog_file, bindings_file, {binding["providerId"]}
        )
    if operation == "github.actions.rerun_failed":
        input_schema["required"] = list(target_fields)
        input_schema["properties"]["expected_workflow_id"]["pattern"] = r"^[0-9]+$"
        catalog_file.write_text(
            json.dumps({"schemaVersion": 1, "items": [catalog_item]}), encoding="utf-8"
        )
        with pytest.raises(preflight.PreflightError, match="numeric workflow ID"):
            preflight._validate_connector_tool_catalog(
                catalog_file, bindings_file, {binding["providerId"]}
            )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("stage1", {"github", "huggingface"}),
        ("stage2", {"github", "huggingface", "azure", "gcp"}),
        ("stage3", {"github", "huggingface", "azure", "gcp", "runpod"}),
    ],
)
def test_connector_provider_allowlist_is_stage_aware(mode: str, expected: set[str]) -> None:
    """Do not let a later cloud provider enter the earlier acceptance stage."""

    assert preflight._expected_connector_providers({}, mode) == expected
    proactive = {"services": {"computer-runtime": {}}}
    assert preflight._expected_connector_providers(proactive, mode) == expected | {"computer"}


def test_github_only_provider_allowlists_exclude_every_hf_binding_and_profile() -> None:
    """Make the explicit no-HF mode an exact GitHub-only connector and Exchange scope."""

    github_only = {
        "services": {
            "account-center": {"labels": {"com.cyrene.huggingface-enabled": "false"}},
            "computer-runtime": {},
        }
    }
    assert preflight._expected_connector_providers(github_only, "stage1") == {
        "github",
        "computer",
    }
    assert preflight._expected_exchange_providers(github_only, "stage1") == set()
    assert preflight._validate_huggingface_profile_selection(github_only, "bootstrap") is False
    assert preflight._validate_huggingface_profile_selection(github_only, "stage1") is False

    with_overlay = json.loads(json.dumps(github_only))
    with_overlay["services"]["huggingface"] = {}
    with pytest.raises(preflight.PreflightError, match="must not include the HF provider service"):
        preflight._validate_huggingface_profile_selection(with_overlay, "stage1")

    enabled_without_overlay = json.loads(json.dumps(github_only))
    enabled_without_overlay["services"]["account-center"]["labels"][
        "com.cyrene.huggingface-enabled"
    ] = "true"
    with pytest.raises(preflight.PreflightError, match="requires compose.huggingface.yaml"):
        preflight._validate_huggingface_profile_selection(enabled_without_overlay, "stage1")

    with pytest.raises(preflight.PreflightError, match="only for bootstrap and stage-1 staging"):
        preflight._validate_huggingface_profile_selection(github_only, "stage2")


def test_exchange_github_only_stage_requires_an_empty_hf_profile_map() -> None:
    """Keep Exchange's authorization profile targets GitHub-only while HF is disabled."""

    document = {
        "platformAuthority": {
            "connectorWorkload": {"connectionRef": "grpcs://workspace-authority:7443"}
        },
        "accountCenter": {
            "connectionRef": "grpcs://account-center:7443",
            "transport": "mtls",
            "callerId": "exchange-service",
            "signingKeyRef": "account/signing",
        },
        "githubIssues": {
            "connectionRef": "grpcs://github-issues:7443",
            "transport": "mtls",
            "callerId": "exchange-service",
            "signingKeyRef": "account/signing",
        },
        "connectorHost": {
            "teamsMessages": {
                "directPlugin": {
                    "connectionRef": "grpcs://teams-provider:7443",
                    "transport": "mtls",
                }
            }
        },
        "authorizationCloud": {"profiles": {}},
    }
    config = {
        "services": {
            "account-center": {"labels": {"com.cyrene.huggingface-enabled": "false"}},
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(document)
                }
            },
        }
    }
    preflight._validate_exchange_profile_stage(config, "stage1")

    document["platformAuthority"]["connectorWorkload"]["connectionRef"] = "grpcs://authority:7443"
    config["services"]["exchange"]["environment"][
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"
    ] = json.dumps(document)
    with pytest.raises(preflight.PreflightError, match="private Authority service"):
        preflight._validate_exchange_profile_stage(config, "stage1")

    document["platformAuthority"]["connectorWorkload"]["connectionRef"] = (
        "grpcs://workspace-authority:7443"
    )
    document["authorizationCloud"]["profiles"]["hf-primary"] = {
        "provider": "hf",
        "directPlugin": {
            "connectionRef": "grpcs://huggingface:7443",
            "transport": "mtls",
        },
    }
    config["services"]["exchange"]["environment"][
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"
    ] = json.dumps(document)
    with pytest.raises(preflight.PreflightError, match=r"must be exactly \[\] for this stage"):
        preflight._validate_exchange_profile_stage(config, "stage1")


def test_huggingface_secrets_are_in_the_optional_inventory_section() -> None:
    """Keep all HF credentials out of the base phase-one secret requirement."""

    inventory = json.loads((DEPLOYMENT_ROOT / "config" / "secret-files.template.json").read_text())
    required = set(inventory["requiredBeforePhase1"])
    optional = set(inventory["requiredBeforeHuggingFace"])
    assert not required & preflight.HUGGINGFACE_OPTIONAL_SECRET_NAMES
    assert optional == preflight.HUGGINGFACE_OPTIONAL_SECRET_NAMES - {
        "account-center-huggingface-oauth-source"
    }
    assert inventory["requiredBeforeHuggingFacePrivateFiles"] == [
        "onboarding/private/huggingface-oauth.json"
    ]


def test_secret_entrypoint_adds_internal_ca_without_replacing_public_roots(
    tmp_path: Path,
) -> None:
    """Keep public trust roots when adding the private Navigator service CA."""

    ca_file = tmp_path / "internal-root-ca.pem"
    ca_bytes = b"-----BEGIN CERTIFICATE-----\nsynthetic internal root\n"
    ca_file.write_bytes(ca_bytes)
    environment = os.environ.copy()
    for name in (
        "CYRENE_SECRET_FILE_MAP",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        environment.pop(name, None)
    environment["CYRENE_INTERNAL_CA_FILE"] = str(ca_file)
    environment["TMPDIR"] = str(tmp_path)
    verify_bundle = (
        "import os, pathlib, sys; "
        "bundle=pathlib.Path(os.environ['SSL_CERT_FILE']); "
        "assert bundle.read_bytes().endswith(pathlib.Path(os.environ['CYRENE_TEST_CA']).read_bytes()); "
        "assert os.environ['REQUESTS_CA_BUNDLE'] == str(bundle); "
        "assert os.environ['NODE_EXTRA_CA_CERTS'] == os.environ['CYRENE_TEST_CA']; "
        "assert bundle.stat().st_mode & 0o077 == 0; "
        "sys.exit(0)"
    )
    environment["CYRENE_TEST_CA"] = str(ca_file)
    result = subprocess.run(
        [
            "sh",
            str(DEPLOYMENT_ROOT / "secret-env-entrypoint.sh"),
            sys.executable,
            "-c",
            verify_bundle,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, "internal CA bundle setup failed"


def _private_https_compose() -> dict[str, Any]:
    """Build one synthetic Compose projection for the internal HTTPS gates."""

    ca = "internal-service-root-ca"
    return {
        "services": {
            "navigator": {
                "environment": {
                    "CYRENE_PERSISTENCE_URL": "https://navigator-persistence:8080",
                    "CYRENE_EXCHANGE_CONNECTOR_INTERNAL_URL": "https://exchange:8081",
                    "NODE_EXTRA_CA_CERTS": "/run/secrets/internal-service-root-ca",
                    "CYRENE_INTERNAL_CA_FILE": "/run/secrets/internal-service-root-ca",
                },
                "secrets": [{"target": ca}],
            },
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_TLS_CERT_FILE": "/run/secrets/exchange-tls-cert",
                    "CYRENE_EXCHANGE_TLS_KEY_FILE": "/run/secrets/exchange-tls-key",
                },
                "secrets": [
                    {"target": "exchange-tls-cert"},
                    {"target": "exchange-tls-key"},
                    {"target": ca},
                ],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "https://exchange:8081/readyz",
                        "/run/secrets/internal-service-root-ca",
                    ]
                },
            },
            "navigator-persistence": {
                "environment": {
                    "CYRENE_PERSISTENCE_TLS_CERT_FILE": "/run/secrets/navigator-persistence-tls-cert",
                    "CYRENE_PERSISTENCE_TLS_KEY_FILE": "/run/secrets/navigator-persistence-tls-key",
                },
                "secrets": [
                    {"target": "navigator-persistence-tls-cert"},
                    {"target": "navigator-persistence-tls-key"},
                    {"target": ca},
                ],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "https://navigator-persistence:8080/readyz",
                        "/run/secrets/internal-service-root-ca",
                    ]
                },
            },
        }
    }


def test_internal_private_apis_require_https_listener_and_ca_mounts() -> None:
    """Reject remote HTTP URLs and missing server certificate or trust mounts."""

    config = _private_https_compose()
    preflight._validate_internal_https_topology(config)

    config["services"]["navigator"]["environment"]["CYRENE_EXCHANGE_CONNECTOR_INTERNAL_URL"] = (
        "http://exchange:8081"
    )
    with pytest.raises(preflight.PreflightError, match="must use HTTPS"):
        preflight._validate_internal_https_topology(config)


def test_internal_service_certificate_validation_binds_san_and_key_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify both private DNS SANs, a single CA chain, and matching leaf keys."""

    commands: list[list[str]] = []

    def fake_run(argv: list[str], *, capture: bool = True) -> str:
        commands.append(argv)
        if "-ext" in argv:
            hostname = (
                "exchange"
                if any("exchange-tls-cert" in item for item in argv)
                else "navigator-persistence"
            )
            return f"X509v3 Subject Alternative Name:\n DNS:{hostname}\n"
        if "pkey" in argv:
            return "matching-public-key\n"
        if "-pubkey" in argv:
            return "matching-public-key\n"
        return "verified\n"

    monkeypatch.setattr(preflight, "_run", fake_run)
    preflight._validate_internal_service_certificates(tmp_path)
    assert any("-checkhost" in command and command[-1] == "exchange" for command in commands)
    assert any(
        "-checkhost" in command and command[-1] == "navigator-persistence" for command in commands
    )

    def mismatched_key(argv: list[str], *, capture: bool = True) -> str:
        if "pkey" in argv:
            return "different-public-key\n"
        return fake_run(argv, capture=capture)

    monkeypatch.setattr(preflight, "_run", mismatched_key)
    with pytest.raises(preflight.PreflightError, match="certificate and private key do not match"):
        preflight._validate_internal_service_certificates(tmp_path)


def test_exchange_authority_mtls_pin_matches_leaf_der_and_valid_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reject a wrong pinned SHA or a client certificate outside Authority's CA."""

    ca_key = tmp_path / "authority-ca.key"
    ca_cert = tmp_path / "authority-ca.pem"
    client_key = tmp_path / "exchange-client.key"
    client_csr = tmp_path / "exchange-client.csr"
    client_cert = tmp_path / "exchange-authority-client-cert"
    commands = [
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-subj",
            "/CN=workspace-authority-test-ca",
            "-days",
            "2",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(client_key),
            "-out",
            str(client_csr),
            "-subj",
            "/CN=exchange",
            "-addext",
            "subjectAltName=DNS:exchange",
        ],
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(client_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(client_cert),
            "-days",
            "1",
            "-copy_extensions",
            "copy",
        ],
    ]
    for command in commands:
        subprocess.run(command, check=True, capture_output=True, text=True)
    (tmp_path / "exchange-authority-client-key").write_bytes(client_key.read_bytes())
    (tmp_path / "authority-tls-client-ca").write_bytes(ca_cert.read_bytes())
    der = subprocess.check_output(["openssl", "x509", "-in", str(client_cert), "-outform", "DER"])
    fingerprint = hashlib.sha256(der).hexdigest()
    config = {
        "services": {
            "workspace-authority": {
                "environment": {"CYRENE_AUTHORITY_EXCHANGE_MTLS_SHA256": fingerprint}
            }
        }
    }

    original_run = preflight._run

    def verify_at_overlapping_certificate_time(argv: list[str], *, capture: bool = True) -> str:
        if argv[:2] != ["openssl", "verify"]:
            return original_run(argv, capture=capture)
        try:
            ca_start = (
                subprocess.check_output(
                    [
                        "openssl",
                        "x509",
                        "-in",
                        str(tmp_path / "authority-tls-client-ca"),
                        "-noout",
                        "-startdate",
                    ],
                    text=True,
                )
                .strip()
                .split("=", 1)[1]
            )
            leaf_start = (
                subprocess.check_output(
                    ["openssl", "x509", "-in", str(client_cert), "-noout", "-startdate"],
                    text=True,
                )
                .strip()
                .split("=", 1)[1]
            )
            ca_end = (
                subprocess.check_output(
                    [
                        "openssl",
                        "x509",
                        "-in",
                        str(tmp_path / "authority-tls-client-ca"),
                        "-noout",
                        "-enddate",
                    ],
                    text=True,
                )
                .strip()
                .split("=", 1)[1]
            )
            leaf_end = (
                subprocess.check_output(
                    ["openssl", "x509", "-in", str(client_cert), "-noout", "-enddate"],
                    text=True,
                )
                .strip()
                .split("=", 1)[1]
            )
        except subprocess.CalledProcessError:
            return original_run(argv, capture=capture)

        def parse(value: str) -> datetime:
            return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)

        attime = max(parse(ca_start), parse(leaf_start)).timestamp() + 1
        latest_valid = min(parse(ca_end), parse(leaf_end)).timestamp()
        assert attime < latest_valid
        result = subprocess.run(
            [*argv[:2], "-attime", str(int(attime)), *argv[2:]],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise preflight.PreflightError("test certificate chain failed OpenSSL verification")
        return result.stdout

    monkeypatch.setattr(preflight, "_run", verify_at_overlapping_certificate_time)

    preflight._validate_exchange_authority_identity(config, tmp_path)

    config["services"]["workspace-authority"]["environment"][
        "CYRENE_AUTHORITY_EXCHANGE_MTLS_SHA256"
    ] = "0" * 64
    with pytest.raises(preflight.PreflightError, match="pin does not match"):
        preflight._validate_exchange_authority_identity(config, tmp_path)

    (tmp_path / "authority-tls-client-ca").write_text("not a certificate", encoding="ascii")
    with pytest.raises(preflight.PreflightError, match="openssl"):
        preflight._validate_exchange_authority_identity(config, tmp_path)


def test_stage_receipt_gate_requires_real_provider_checks_and_cleaned_ledgers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require the complete receipt chain before stage 2, stage 3, or cloud cutover."""

    task_id = "cloud-task-20261023"
    owner = UUID
    instant = "2026-10-23T12:00:00Z"
    providers = {
        name: {
            "identityStatus": "VERIFIED",
            "privateReadStatus": "VERIFIED",
            "writeReadbackStatus": "VERIFIED",
            "cleanupReadbackStatus": "ABSENT",
            "resourceRef": f"{name}-private-resource",
            "writeReadbackSha256": "a" * 64,
            "cleanupReadbackAt": instant,
        }
        for name in ("github", "huggingface")
    }
    stage1 = {
        "schemaVersion": "cyrene-cloud-acceptance-evidence-v1",
        "stage": "github_hf",
        "status": "VERIFIED",
        "evidenceClass": "REAL_PROVIDER_API",
        "verifiedAt": instant,
        "taskId": task_id,
        "owner": owner,
        "reservedUsd": 0.0,
        "cumulativeReservedUsd": 0.0,
        "providers": providers,
    }
    stage1_bytes = json.dumps(stage1, sort_keys=True).encode()
    stage2_api = _cloud_api_receipt(
        "azure_gcp", ("azure", "gcp"), task_id, owner, stage1_bytes, 0.0, 1.5, instant
    )
    stage2_api_bytes = json.dumps(stage2_api, sort_keys=True).encode()
    stage2_cli_bytes = json.dumps(
        {
            "schemaVersion": "cyrene-cloud-acceptance-ledger-v1",
            "stage": "azure_gcp",
            "status": "VERIFIED",
            "evidenceClass": "REAL_VENDOR_CLI",
            "taskId": task_id,
            "owner": owner,
            "budgetUsd": 5.0,
            "reservedUsd": 1.0,
            "cumulativeReservedUsd": 1.0,
            "previousEvidenceSha256": hashlib.sha256(stage1_bytes).hexdigest(),
            "verifiedAt": instant,
            "cleanupVerifiedAt": instant,
            "resources": {
                f"{provider}-resource": {
                    "provider": provider,
                    "state": "DELETED",
                    "ownership": {
                        "taskId": task_id,
                        "owner": owner,
                        "marker": f"cyrene-teams-cloud-{task_id}-{provider}",
                    },
                    "events": [
                        {
                            "action": "verify-cleanup-absence",
                            "outcome": "EXITED",
                            "exitCode": 0,
                        }
                    ],
                }
                for provider in ("azure", "gcp")
            },
        },
        sort_keys=True,
    ).encode()
    stage2_cli = json.loads(stage2_cli_bytes)
    stage3_api = _cloud_api_receipt(
        "runpod", ("runpod",), task_id, owner, stage2_api_bytes, 1.5, 1.0, instant
    )
    stage3_api_bytes = json.dumps(stage3_api, sort_keys=True).encode()
    stage3_cli_bytes = json.dumps(
        {
            "schemaVersion": "cyrene-cloud-acceptance-ledger-v1",
            "stage": "runpod",
            "status": "VERIFIED",
            "evidenceClass": "REAL_VENDOR_CLI",
            "taskId": task_id,
            "owner": owner,
            "budgetUsd": 5.0,
            "reservedUsd": 0.5,
            "cumulativeReservedUsd": 1.5,
            "previousEvidenceSha256": hashlib.sha256(stage2_cli_bytes).hexdigest(),
            "verifiedAt": instant,
            "cleanupVerifiedAt": instant,
            "resources": {
                "runpod-resource": {
                    "provider": "runpod",
                    "state": "DELETED",
                    "ownership": {
                        "taskId": task_id,
                        "owner": owner,
                        "marker": f"cyrene-teams-cloud-{task_id}-runpod",
                    },
                    "events": [
                        {
                            "action": "verify-cleanup-absence",
                            "outcome": "EXITED",
                            "exitCode": 0,
                        }
                    ],
                }
            },
        },
        sort_keys=True,
    ).encode()
    receipts = {
        "github-hf.json": (stage1, stage1_bytes),
        "azure-gcp-cli.json": (stage2_cli, stage2_cli_bytes),
        "azure-gcp-api.json": (stage2_api, stage2_api_bytes),
        "runpod-cli.json": (json.loads(stage3_cli_bytes), stage3_cli_bytes),
        "runpod-api.json": (json.loads(stage3_api_bytes), stage3_api_bytes),
    }
    monkeypatch.setattr(
        preflight,
        "_read_private_receipt",
        lambda path, _label: receipts[path.name],
    )
    config = {
        "services": {
            "directory-operator": {
                "environment": {"CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID": owner}
            },
            "cloud-management-phase2": {"profiles": ["cloud-phase2", "runpod"]},
        }
    }
    github_path = Path("/private/github-hf.json")
    azure_cli_path = Path("/private/azure-gcp-cli.json")
    azure_api_path = Path("/private/azure-gcp-api.json")
    runpod_api_path = Path("/private/runpod-api.json")

    with pytest.raises(preflight.PreflightError, match="requires verified GitHub/Hugging Face"):
        preflight._validate_stage_acceptance_receipts(
            mode="stage2",
            config=config,
            github_hf_evidence=None,
            azure_gcp_ledger=None,
            runpod_ledger=None,
        )
    preflight._validate_stage_acceptance_receipts(
        mode="stage2",
        config=config,
        github_hf_evidence=github_path,
        azure_gcp_ledger=None,
        runpod_ledger=None,
    )
    with pytest.raises(preflight.PreflightError, match="requires verified Azure/GCP provider API"):
        preflight._validate_stage_acceptance_receipts(
            mode="stage3",
            config=config,
            github_hf_evidence=github_path,
            azure_gcp_ledger=None,
            azure_gcp_api_evidence=None,
            runpod_ledger=None,
        )
    preflight._validate_stage_acceptance_receipts(
        mode="stage3",
        config=config,
        github_hf_evidence=github_path,
        azure_gcp_ledger=None,
        azure_gcp_api_evidence=azure_api_path,
        runpod_ledger=None,
    )
    with pytest.raises(
        preflight.PreflightError, match="RunPod cutover requires verified RunPod API evidence"
    ):
        preflight._validate_stage_acceptance_receipts(
            mode="cutover",
            config=config,
            github_hf_evidence=github_path,
            azure_gcp_ledger=None,
            azure_gcp_api_evidence=azure_api_path,
            runpod_ledger=None,
        )
    preflight._validate_stage_acceptance_receipts(
        mode="cutover",
        config=config,
        github_hf_evidence=github_path,
        azure_gcp_ledger=None,
        azure_gcp_api_evidence=azure_api_path,
        runpod_ledger=None,
        runpod_api_evidence=runpod_api_path,
    )

    with pytest.raises(
        preflight.PreflightError, match="vendor-CLI mutation ledgers are not accepted"
    ):
        preflight._validate_stage_acceptance_receipts(
            mode="stage3",
            config=config,
            github_hf_evidence=github_path,
            azure_gcp_ledger=azure_cli_path,
            azure_gcp_api_evidence=azure_api_path,
            runpod_ledger=None,
        )

    broken_api = json.loads(stage2_api_bytes)
    broken_api["providers"][0]["resources"][0]["cleanupAbsenceStatus"] = "STOPPED"
    receipts["azure-gcp-api.json"] = (
        broken_api,
        json.dumps(broken_api, sort_keys=True).encode(),
    )
    with pytest.raises(preflight.PreflightError, match="ownership or verified cleanup absence"):
        preflight._validate_stage_acceptance_receipts(
            mode="stage3",
            config=config,
            github_hf_evidence=github_path,
            azure_gcp_ledger=None,
            azure_gcp_api_evidence=azure_api_path,
            runpod_ledger=None,
        )


def _cloud_api_receipt(
    stage: str,
    providers: tuple[str, ...],
    task_id: str,
    owner: str,
    previous_evidence: bytes,
    previous_reserved: float,
    reserved: float,
    instant: str,
) -> dict[str, Any]:
    """Create a closed synthetic provider API receipt for gate tests."""

    provider_rows = []
    for provider in providers:
        provider_rows.append(
            {
                "providerId": provider,
                "identityStatus": "VERIFIED",
                "authMode": "managed_identity",
                "profileScopeDigestSha256": "b" * 64,
                "resources": [
                    {
                        "ownership": {
                            "taskId": task_id,
                            "owner": owner,
                            "marker": f"cyrene-teams-cloud-{task_id}-{provider}",
                        },
                        "vendorRef": f"{provider}-resource-ref",
                        "desiredState": "DELETED",
                        "actualState": "ABSENT",
                        "readiness": "NOT_READY",
                        "cleanupAbsenceStatus": "ABSENT",
                        "cleanupReadbackAt": instant,
                    }
                ],
                "writeOperations": [
                    {
                        "toolId": "cloud.resource.create",
                        "operationId": "cloud.resource.create",
                        "requestDigestSha256": "c" * 64,
                        "resourceRef": f"{provider}-resource-ref",
                        "grantRef": "grant-ref",
                        "workOperationId": "work-operation-id",
                        "outcome": "SUCCEEDED",
                        "operationRef": "operation-ref",
                        "stateReadback": {
                            "status": "VERIFIED",
                            "observedAt": instant,
                            "stateDigestSha256": "d" * 64,
                        },
                        "resultDigestSha256": "e" * 64,
                    }
                ],
            }
        )
    return {
        "schemaVersion": "cyrene-cloud-api-evidence-v1",
        "taskId": task_id,
        "owner": owner,
        "stage": stage,
        "status": "VERIFIED",
        "evidenceClass": "REAL_PROVIDER_API",
        "executionPath": "navigator_dsh_exchange_directplugin_api",
        "verifiedAt": instant,
        "reservedUsd": reserved,
        "cumulativeReservedUsd": previous_reserved + reserved,
        "previousEvidenceSha256": hashlib.sha256(previous_evidence).hexdigest(),
        "providers": provider_rows,
    }


def test_cloud_api_receipt_rejects_cli_substitution_wrong_chain_and_open_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require a closed DSH API receipt, its exact previous hash, and provider cleanup."""

    previous = b'{"stage":"github_hf","status":"VERIFIED"}'
    evidence = _cloud_api_receipt(
        "azure_gcp",
        ("azure", "gcp"),
        "cloud-task-20261023",
        UUID,
        previous,
        0.0,
        1.25,
        "2026-10-23T12:00:00Z",
    )
    payload = json.dumps(evidence, sort_keys=True).encode()
    monkeypatch.setattr(
        preflight, "_read_private_receipt", lambda _path, _label: (evidence, payload)
    )
    preflight._validate_cloud_api_evidence(
        Path("/private/cloud-api.json"),
        expected_stage="azure_gcp",
        expected_providers={"azure", "gcp"},
        expected_owner=UUID,
        expected_task_id="cloud-task-20261023",
        previous_evidence=previous,
        previous_reserved_usd=0.0,
    )

    for mutation in (
        lambda row: row.update(evidenceClass="REAL_VENDOR_CLI"),
        lambda row: row.update(previousEvidenceSha256="0" * 64),
        lambda row: row.update(unreviewedField=True),
        lambda row: row["providers"][0].update(
            resources=[{**row["providers"][0]["resources"][0], "cleanupAbsenceStatus": "STOPPED"}]
        ),
    ):
        invalid = json.loads(json.dumps(evidence))
        mutation(invalid)
        invalid_payload = json.dumps(invalid, sort_keys=True).encode()
        monkeypatch.setattr(
            preflight,
            "_read_private_receipt",
            lambda _path, _label, row=invalid, raw=invalid_payload: (row, raw),
        )
        with pytest.raises(preflight.PreflightError):
            preflight._validate_cloud_api_evidence(
                Path("/private/cloud-api.json"),
                expected_stage="azure_gcp",
                expected_providers={"azure", "gcp"},
                expected_owner=UUID,
                expected_task_id="cloud-task-20261023",
                previous_evidence=previous,
                previous_reserved_usd=0.0,
            )


def test_memavailable_distinguishes_staging_measurement_from_cutover() -> None:
    """Allow measured staging within the host reserve but block a phase that cannot fit."""

    cloud = {"mem_limit": 384 * 1024**2}
    config = {"services": {"cloud-management-phase2": cloud}}
    stage1 = preflight._memory_headroom(
        mode="stage1",
        config=config,
        available_mib=2263,
        cgroup_budget_mib=3008,
        minimum_available_mib=1024,
    )
    assert stage1["status"] == "READY_FOR_STAGING_MEASUREMENT"
    assert stage1["runtimeRssMeasurementRequired"] is True

    with pytest.raises(preflight.PreflightError, match="requires 1408 MiB"):
        preflight._memory_headroom(
            mode="stage2",
            config=config,
            available_mib=1407,
            cgroup_budget_mib=3392,
            minimum_available_mib=1024,
        )
    cutover = preflight._memory_headroom(
        mode="cutover",
        config=config,
        available_mib=1024,
        cgroup_budget_mib=3392,
        minimum_available_mib=1024,
    )
    assert cutover["status"] == "READY"


def test_cutover_runtime_requires_healthy_services_and_records_rss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require actual staged containers before cutover and return measured service RSS."""

    config = {
        "services": {
            "navigator": {"healthcheck": {"test": ["CMD", "node"]}},
            "teams-public-ingress": {
                "profiles": ["public-ingress"],
                "healthcheck": {"test": ["CMD", "python"]},
            },
            "directory-operator": {"profiles": ["directory-operator"], "restart": "no"},
        }
    }
    outputs = {
        "ps": "cyrene-teams-cloud-navigator-1|Up 2 minutes (healthy)\n"
        "cyrene-teams-cloud-teams-public-ingress-1|Up 2 minutes (healthy)\n",
        "stats": "cyrene-teams-cloud-navigator-1|312.4MiB / 768MiB\n"
        "cyrene-teams-cloud-teams-public-ingress-1|24MiB / 128MiB\n",
    }

    def fake_run(argv: list[str], *, capture: bool = True) -> str:
        return outputs["stats" if "stats" in argv else "ps"]

    monkeypatch.setattr(preflight, "_run", fake_run)
    usage = preflight._validate_cutover_runtime(config, ["docker"], "cutover")
    assert usage == {"navigator": pytest.approx(312.4), "teams-public-ingress": pytest.approx(24.0)}

    outputs["ps"] = (
        "cyrene-teams-cloud-navigator-1|Up 2 minutes (unhealthy)\n"
        "cyrene-teams-cloud-teams-public-ingress-1|Up 2 minutes (healthy)\n"
    )
    with pytest.raises(preflight.PreflightError, match="must pass their health checks"):
        preflight._validate_cutover_runtime(config, ["docker"], "cutover")


def _exchange_configuration(providers: set[str]) -> dict[str, Any]:
    """Return one synthetic connector-only Exchange configuration for a phase."""

    cloud_target = {
        "bindingId": "host-cloud-management-binding",
        "connectionRef": "grpcs://cloud-management:7443",
        "transport": "mtls",
    }
    profile_templates = {
        "hf": {
            "provider": "hf",
            "directPlugin": {
                "bindingId": "host-huggingface-provider-binding",
                "connectionRef": "grpcs://huggingface:7443",
                "transport": "mtls",
            },
        },
        "azure": {"provider": "azure", "directPlugin": cloud_target},
        "gcp": {"provider": "gcp", "directPlugin": cloud_target},
        "runpod": {"provider": "runpod", "directPlugin": cloud_target},
    }
    return {
        "schemaVersion": 1,
        "platformAuthority": {
            "connectorWorkload": {"connectionRef": "grpcs://workspace-authority:7443"}
        },
        "accountCenter": {
            "bindingId": "host-account-center-binding",
            "connectionRef": "grpcs://account-center:7443",
            "transport": "mtls",
            "callerId": "exchange-service",
            "signingKeyRef": "account/signing",
        },
        "githubIssues": {
            "bindingId": "host-github-issues-binding",
            "connectionRef": "grpcs://github-issues:7443",
            "transport": "mtls",
            "callerId": "exchange-service",
            "signingKeyRef": "account/signing",
        },
        "connectorHost": {
            "teamsMessages": {
                "directPlugin": {
                    "bindingId": "host-teams-message-binding",
                    "connectionRef": "grpcs://teams-provider:7443",
                    "transport": "mtls",
                }
            }
        },
        "authorizationCloud": {
            "profiles": {
                f"{provider}-primary": profile_templates[provider] for provider in sorted(providers)
            }
        },
    }


@pytest.mark.parametrize("huggingface_enabled", [False, True])
def test_real_compose_render_passes_codebuddy_computer_profile_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, huggingface_enabled: bool
) -> None:
    """Feed actual Compose-rendered values into the ACP, HF, and computer preflight gates."""

    docker = preflight.shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose config is required for rendered-profile validation")
    safe_environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "DOCKER_CONFIG": str(tmp_path / "docker-config"),
    }
    Path(safe_environment["HOME"]).mkdir()
    Path(safe_environment["DOCKER_CONFIG"]).mkdir()
    version = subprocess.run(
        [docker, "compose", "version"],
        capture_output=True,
        check=False,
        env=safe_environment,
        text=True,
    )
    if version.returncode != 0:
        pytest.skip("Docker Compose plugin is not installed")

    config_dir = tmp_path / "config"
    secrets_dir = tmp_path / "secrets"
    codebuddy_home = secrets_dir / "codebuddy"
    config_dir.mkdir()
    secrets_dir.mkdir()
    codebuddy_home.mkdir()
    caddyfile = tmp_path / "Caddyfile.computer-runtime"
    caddyfile.write_text(
        "mode require_and_verify\n"
        "trust_pool file /run/secrets/exchange-plugin-client-ca\n"
        "reverse_proxy h2c://computer-runtime:50051\n",
        encoding="utf-8",
    )
    staging_caddyfile = tmp_path / "Caddyfile.staging"
    staging_caddyfile.write_text("# synthetic Compose config probe\n", encoding="utf-8")
    responsibility_targets = config_dir / "responsibility-targets.v1.json"
    responsibility_targets.write_text("{}\n", encoding="utf-8")
    oauth_source = tmp_path / "huggingface-oauth.json"
    oauth_source.write_text("{}\n", encoding="utf-8")

    tenant_id = "00000000-0000-4000-8000-000000000003"
    organization_id = "00000000-0000-4000-8000-000000000001"
    workspace_id = "00000000-0000-4000-8000-000000000002"
    integration = _exchange_configuration({"hf"} if huggingface_enabled else set())
    integration["hostSecretRefs"] = {
        "tls/plugin-root": "EXCHANGE_COMPUTER_PROXY_ROOT_CA_FILE",
        "tls/exchange-plugin-client-cert": "EXCHANGE_COMPUTER_PROXY_CLIENT_CERT_FILE",
        "tls/exchange-plugin-client-key": "EXCHANGE_COMPUTER_PROXY_CLIENT_KEY_FILE",
    }
    integration["connectorHost"]["computerRunner"] = {
        "profileRef": "computer-workspace-main",
        "connectionRef": "00000000-0000-4000-8000-000000000004",
        "organizationId": organization_id,
        "workspaceId": workspace_id,
        "expectedIssuer": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        "directPlugin": {
            "bindingId": "computer-runtime",
            "connectionRef": "grpcs://computer-runtime-proxy:7443",
            "transport": "mtls",
            "mtls": {
                "rootCertificateRef": "tls/plugin-root",
                "clientCertificateRef": "tls/exchange-plugin-client-cert",
                "clientKeyRef": "tls/exchange-plugin-client-key",
            },
        },
        "maxCommandTimeoutMs": 600000,
        "maxFileBytes": 1048576,
        "maxReadBytes": 65536,
        "maxDirectoryDepth": 32,
    }

    compose_files = [
        DEPLOYMENT_ROOT / "compose.yaml",
        *([DEPLOYMENT_ROOT / "compose.huggingface.yaml"] if huggingface_enabled else []),
        DEPLOYMENT_ROOT / "compose.computer-runtime.yaml",
        DEPLOYMENT_ROOT / "compose.codebuddy-acp.yaml",
    ]
    compose_text = "\n".join(path.read_text(encoding="utf-8") for path in compose_files)
    variable_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_text))
    values: dict[str, str] = {}
    values.update(
        {
            "CYRENE_HF_ENABLED": "true" if huggingface_enabled else "false",
            "CYRENE_CONFIG_DIR": str(config_dir),
            "CYRENE_SECRETS_DIR": str(secrets_dir),
            "CYRENE_CODEBUDDY_HOME": str(codebuddy_home),
            "CYRENE_CADDYFILE": str(staging_caddyfile),
            "CYRENE_COMPUTER_CADDYFILE": str(caddyfile),
            "CYRENE_RESPONSIBILITY_TARGETS_HOST_FILE": str(responsibility_targets),
            "CYRENE_HF_OAUTH_BOOTSTRAP_FILE": str(oauth_source),
            "CYRENE_HARNESS_CODEBUDDY_ACP_COMMAND": "codebuddy",
            "CYRENE_HARNESS_CODEBUDDY_ACP_MODEL": "glm-5.3-flash",
            "CYRENE_HARNESS_CODEBUDDY_FALLBACK_ENABLED": "false",
            "CYRENE_HARNESS_CODEBUDDY_NO_PAID_TOPUP": "true",
            "CYRENE_HARNESS_CODEBUDDY_FALLBACK_MODEL": "",
            "CYRENE_HARNESS_MODEL": "",
            "CYRENE_HARNESS_MODEL_SOURCE": "",
            "CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS": "2",
            "CYRENE_WORKFLOWS_ENABLED": "false",
            "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                integration, separators=(",", ":")
            ),
            "CYRENE_ACCOUNT_CENTER_CONFIGURATION_JSON": "{}",
            "CYRENE_GITHUB_ISSUES_CONFIGURATION_JSON": "{}",
            "CYRENE_TEAMS_CONNECTOR_CONFIGURATION_JSON": "{}",
            "CYRENE_HUGGINGFACE_CONFIGURATION_JSON": "{}",
            "CYRENE_ORGANIZATION_ID": organization_id,
            "CYRENE_WORKSPACE_ID": workspace_id,
            "CYRENE_AUTHORITY_AAD_TENANT_ID": tenant_id,
            "TEAMS_TENANT_ID": tenant_id,
            "CYRENE_PUBLIC_BIND": "127.0.0.1",
            "CYRENE_HTTPS_PORT": "18443",
            "CYRENE_AUTHORITY_EXCHANGE_MTLS_SHA256": "a" * 64,
            "CYRENE_TEAMS_EXCHANGE_CONNECTION_REF": "00000000-0000-4000-8000-000000000020",
            "CYRENE_TEAMS_EXCHANGE_PROFILE_REF": "teams-primary",
            "CYRENE_COMPUTER_COMMAND_DEFAULT_TIMEOUT_MS": "30000",
            "CYRENE_COMPUTER_COMMAND_MAX_TIMEOUT_MS": "600000",
            "CYRENE_COMPUTER_MAX_OUTPUT_BYTES": "65536",
            "CYRENE_COMPUTER_MAX_FILE_BYTES": "1048576",
            "CYRENE_COMPUTER_MAX_DIRECTORY_ENTRIES": "2048",
            "CYRENE_COMPUTER_MAX_DIRECTORY_DEPTH": "32",
            "CYRENE_COMPUTER_MAX_ARTIFACT_BYTES": "16777216",
        }
    )
    for name in variable_names:
        if name in values:
            continue
        if "IMAGE" in name or name == "CYRENE_CADDY_IMAGE" or name == "CYRENE_POSTGRES_IMAGE":
            values[name] = "ghcr.io/cyrene/compose-probe:latest"
        elif name.endswith("_JSON") and name not in values:
            values[name] = "{}"
        elif name.endswith("_ID"):
            values[name] = "00000000-0000-4000-8000-000000000010"
        elif name.endswith("_SHA256"):
            values[name] = "a" * 64
        elif name in {"TEAMS_FQDN"}:
            values[name] = "teams.example.invalid"
        elif name in {"TEAMS_ALLOWED_TEAM_IDS"}:
            values[name] = "team-probe"
        elif name.endswith("_PORT"):
            values[name] = "18443"
        elif name.endswith(("_DIR", "_FILE")) or "CADDYFILE" in name:
            path = tmp_path / name.lower()
            path.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith("_DIR"):
                path.mkdir(exist_ok=True)
            else:
                path.touch(exist_ok=True)
            values[name] = str(path)
        else:
            values[name] = "synthetic-value"

    for compose_file in compose_files:
        document = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        for section in ("secrets", "configs"):
            for row in document.get(section, {}).values():
                template_path = row.get("file") if isinstance(row, dict) else None
                if not isinstance(template_path, str):
                    continue
                resolved = re.sub(
                    r"\$\{([A-Z][A-Z0-9_]*)(?::[^}]*)?\}",
                    lambda match: values[match.group(1)],
                    template_path,
                )
                path = Path(resolved)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)

    env_file = tmp_path / "compose-probe.env"
    env_file.write_text(
        "".join(f"{name}={value}\n" for name, value in sorted(values.items())),
        encoding="utf-8",
    )
    isolated_environment = dict(safe_environment)

    def compose_config(argv: list[str], *, capture: bool = True) -> str:
        result = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            env=isolated_environment,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    monkeypatch.setattr(preflight, "_run", compose_config)
    rendered = preflight._compose_config([docker], compose_files, [env_file])
    assert ("huggingface" in rendered["services"]) is huggingface_enabled
    authority_service = rendered["services"]["workspace-authority"]
    exchange_service = rendered["services"]["exchange"]
    authority_networks = authority_service["networks"]
    exchange_networks = exchange_service["networks"]
    assert "exchange-authority-private" in authority_networks
    assert "exchange-authority-private" in exchange_networks
    assert "authority" not in authority_networks["exchange-authority-private"].get("aliases", [])
    assert "authority" in authority_networks["directory-private"].get("aliases", [])
    rendered_exchange_configuration = json.loads(
        exchange_service["environment"]["CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"]
    )
    authority_connection_ref = rendered_exchange_configuration["platformAuthority"][
        "connectorWorkload"
    ]["connectionRef"]
    assert authority_connection_ref == "grpcs://workspace-authority:7443"
    authority_hostname = authority_connection_ref.split("://", 1)[1].split(":", 1)[0]
    assert authority_hostname == "workspace-authority"
    assert exchange_service["depends_on"]["workspace-authority"]["condition"] == "service_healthy"
    runner = rendered["services"]["computer-runtime"]
    proxy = rendered["services"]["computer-runtime-proxy"]
    assert preflight._memory_limit_bytes(runner["mem_limit"]) == 512 * preflight.MIB
    assert preflight._cpu_limit_decimal(runner["cpus"]) == preflight.Decimal("0.10")
    assert preflight._memory_limit_bytes(proxy["mem_limit"]) == 64 * preflight.MIB
    assert preflight._cpu_limit_decimal(proxy["cpus"]) == preflight.Decimal("0.05")
    preflight._validate_profile_stage(rendered, "stage1")
    preflight._validate_codebuddy_acp_boundary(
        rendered, secrets_dir=secrets_dir, config_dir=config_dir
    )


def test_exchange_cloud_profiles_follow_compose_overlay_stage() -> None:
    """Keep Azure/GCP and RunPod absent from earlier Exchange profile maps."""

    phase1 = {
        "services": {
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                        _exchange_configuration({"hf"})
                    )
                }
            }
        }
    }
    preflight._validate_exchange_profile_stage(phase1, "stage1")

    phase2 = {
        "services": {
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                        _exchange_configuration({"hf", "azure", "gcp"})
                    )
                }
            },
            "cloud-management-phase2": {},
        }
    }
    preflight._validate_exchange_profile_stage(phase2, "stage2")
    with pytest.raises(preflight.PreflightError, match="exactly "):
        preflight._validate_exchange_profile_stage(phase2, "stage1")

    phase3 = {
        "services": {
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                        _exchange_configuration({"hf", "azure", "gcp", "runpod"})
                    )
                }
            },
            "cloud-management-phase2": {"profiles": ["cloud-phase2", "runpod"]},
        }
    }
    preflight._validate_exchange_profile_stage(phase3, "stage3")


def test_stage3_reuses_one_cloud_provider_state_owner() -> None:
    """Add RunPod to the phase-2 service instead of starting a second volume writer."""

    config = {
        "services": {
            "account-center": {"labels": {"com.cyrene.huggingface-enabled": "true"}},
            "huggingface": {},
            "exchange": {
                "environment": {
                    "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                        _exchange_configuration({"hf", "azure", "gcp", "runpod"})
                    )
                }
            },
            "cloud-management-phase2": {
                "profiles": ["cloud-phase2", "runpod"],
                "environment": {
                    "AZURE_CLIENT_ID": UUID,
                    "CYRENE_CLOUD_MGMT_AZURE_SUBSCRIPTION_ID": UUID,
                    "CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE": "/run/secrets/runpod-api-key",
                    "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
                        {
                            "profiles": [
                                {
                                    "provider": "azure",
                                    "auth": {"mode": "managed_identity", "client_id": UUID},
                                    "scope": {"subscription_id": UUID},
                                },
                                {"provider": "gcp"},
                                {"provider": "runpod"},
                            ]
                        }
                    ),
                },
            },
        }
    }
    preflight._validate_profile_stage(config, "stage3")
    assert "cloud-management-runpod" not in config["services"]
    config["services"]["cloud-management-phase2"]["environment"][
        "CYRENE_CLOUD_MGMT_AZURE_SUBSCRIPTION_ID"
    ] = "00000000-0000-4000-8000-000000000002"
    with pytest.raises(preflight.PreflightError, match="must match"):
        preflight._validate_profile_stage(config, "stage3")


def test_public_caddyfile_is_callback_only_and_tls_alpn_only() -> None:
    """Require the public ingress route to expose only the Bot callback over TLS-ALPN."""

    source = str((DEPLOYMENT_ROOT / "Caddyfile").resolve())
    service = {"volumes": [{"type": "bind", "source": source, "target": "/etc/caddy/Caddyfile"}]}
    preflight._validate_caddyfile(service, "cutover")

    staging = str((DEPLOYMENT_ROOT / "Caddyfile.staging").resolve())
    service["volumes"][0]["source"] = staging
    with pytest.raises(preflight.PreflightError, match="canonical Caddyfile"):
        preflight._validate_caddyfile(service, "cutover")


def test_teams_provider_uses_only_the_file_backed_bot_secret() -> None:
    """Pin the egress connector to the rotated secret file and approved Team scope."""

    service = {
        "environment": {
            "CYRENE_CAPABILITY_BINDING_ID": "host-teams-message-binding",
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": (
                '{"bot_app_id":"bot-id","tenant_id":"tenant-id",'
                '"allowed_team_ids":["team-id"],'
                '"client_secret_env_ref":"TEAMS_BOT_CLIENT_SECRET"}'
            ),
            "CYRENE_SECRET_FILE_MAP": (
                "TEAMS_BOT_CLIENT_SECRET=/run/secrets/teams-bot-client-secret"
            ),
        },
        "secrets": [
            {
                "source": "teams-bot-client-secret",
                "target": "teams-bot-client-secret",
            }
        ],
    }
    navigator = {
        "environment": {
            "TEAMS_BOT_APP_ID": "bot-id",
            "TEAMS_TENANT_ID": "tenant-id",
            "TEAMS_ALLOWED_TEAM_IDS": "team-id",
        }
    }
    preflight._validate_teams_connector_config(service, navigator)

    service["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = service["environment"][
        "CYRENE_CAPABILITY_CONFIGURATION_JSON"
    ].replace("TEAMS_BOT_CLIENT_SECRET", "TEAMS_BOT_APP_SECRET")
    with pytest.raises(preflight.PreflightError, match="secret reference"):
        preflight._validate_teams_connector_config(service, navigator)


def _account_center_oauth_service() -> dict[str, Any]:
    """Return a secret-free Account Center activation shape for device OAuth."""

    config = {
        "github_app_id": "544865",
        "github_app_private_key_ref": "github-app/private-key",
        "encryption_key_ref": "account-center/encryption-key",
        "caller_key_refs": {"exchange-service": "account/signing"},
        "hostSecretRefs": {
            "github-app/private-key": "CYRENE_ACCOUNT_CENTER_GITHUB_APP_PRIVATE_KEY",
            "account-center/encryption-key": "CYRENE_ACCOUNT_CENTER_ENCRYPTION_KEY",
            "account/signing": "CYRENE_ACCOUNT_CENTER_EXCHANGE_HMAC_KEY",
        },
        "provider_secret_file_refs": ["huggingface/connector-token"],
        "hostSecretFileRefs": {
            "huggingface/connector-token": "/run/secrets/huggingface-oauth.json"
        },
    }
    return {
        "environment": {
            "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(config),
            "CYRENE_SECRET_FILE_MAP": ",".join(
                f"{value}=/run/secrets/{name.rsplit('/', 1)[-1]}"
                for name, value in config["hostSecretRefs"].items()
            ),
        }
    }


def test_account_center_uses_oauth_file_reference_and_no_hf_token_env() -> None:
    """Require the OAuth seed file ref and keep it out of environment secret maps."""

    service = _account_center_oauth_service()
    preflight._validate_account_center_config(service)

    config = json.loads(service["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"])
    config["provider_secret_refs"] = ["huggingface/fine-grained-token"]
    service["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    with pytest.raises(preflight.PreflightError, match="must not be copied"):
        preflight._validate_account_center_config(service)


def test_account_center_accepts_github_only_config_without_hf_sources_or_mounts() -> None:
    """Allow Account Center to bootstrap GitHub when no HF authorization exists."""

    service = _account_center_oauth_service()
    config = json.loads(service["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"])
    config["provider_secret_file_refs"] = []
    config["hostSecretFileRefs"] = {}
    service["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    service["secrets"] = []
    service["networks"] = {"account-private": {}, "github-account-center-private": {}}
    preflight._validate_account_center_config(service, huggingface_enabled=False)

    config["provider_secret_file_refs"] = ["huggingface/connector-token"]
    config["hostSecretFileRefs"] = {
        "huggingface/connector-token": "/run/secrets/huggingface-oauth.json"
    }
    service["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    with pytest.raises(preflight.PreflightError, match="no Hugging Face refs or profiles"):
        preflight._validate_account_center_config(service, huggingface_enabled=False)

    config["provider_secret_file_refs"] = []
    config["hostSecretFileRefs"] = {}
    service["environment"]["CYRENE_CAPABILITY_CONFIGURATION_JSON"] = json.dumps(config)
    service["environment"]["CYRENE_SECRET_FILE_MAP"] += (
        ",CYRENE_ACCOUNT_CENTER_HF_PROVIDER_HMAC_KEY=/run/secrets/account-center-hf-provider-hmac-key"
    )
    service["secrets"] = [
        {
            "source": "account-center-hf-provider-hmac-key",
            "target": "account-center-hf-provider-hmac-key",
        }
    ]
    with pytest.raises(preflight.PreflightError, match="must not mount Hugging Face secrets"):
        preflight._validate_account_center_config(service, huggingface_enabled=False)


def test_connector_bootstrap_input_must_match_the_selected_provider_set(tmp_path: Path) -> None:
    """Accept the real GitHub-only Exchange input and reject HF target leakage."""

    document = {
        "schemaVersion": 1,
        "actor": {
            "issuer": "https://login.microsoftonline.com/00000000-0000-4000-8000-000000000001/v2.0",
            "subject": UUID,
            "organizationId": "organization-test",
            "workspaceId": "workspace-test",
        },
        "github": {
            "installationId": "12345",
            "expectedAccessMode": "workspace_operator",
            "profileIdempotencyKey": "teams-cloud-github-installation",
            "repositories": [
                {"repositoryId": "123456789", "owner": "DoHorizon-AI", "name": "Cyrene"}
            ],
        },
    }
    path = tmp_path / "connector-bootstrap.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    expected_actor = preflight._connector_bootstrap_actor(
        {
            "services": {
                "navigator": {
                    "environment": {"TEAMS_TENANT_ID": "00000000-0000-4000-8000-000000000001"}
                },
                "directory-operator": {
                    "environment": {
                        "CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID": UUID,
                    }
                },
                "workspace-authority": {
                    "environment": {
                        "CYRENE_ORGANIZATION_ID": "organization-test",
                        "CYRENE_WORKSPACE_ID": "workspace-test",
                    }
                },
            }
        }
    )
    assert expected_actor == document["actor"]
    preflight._validate_connector_bootstrap_config(
        path, huggingface_enabled=False, expected_actor=expected_actor
    )
    with pytest.raises(
        preflight.PreflightError, match="approved Directory owner and CYRENE_HF_ENABLED"
    ):
        preflight._validate_connector_bootstrap_config(
            path, huggingface_enabled=True, expected_actor=expected_actor
        )

    document["actor"]["subject"] = "00000000-0000-4000-8000-000000000099"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="approved Directory owner"):
        preflight._validate_connector_bootstrap_config(
            path, huggingface_enabled=False, expected_actor=expected_actor
        )
    document["actor"]["subject"] = expected_actor["subject"]

    document["huggingface"] = {
        "profileAlias": "hf-primary",
        "targetAlias": "hf-primary-target",
        "expectedAccessMode": "workspace_operator",
        "profileIdempotencyKey": "teams-cloud-huggingface-primary",
        "credentialSecretRef": "huggingface/connector-token",
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="approved Directory owner"):
        preflight._validate_connector_bootstrap_config(
            path, huggingface_enabled=False, expected_actor=expected_actor
        )
    preflight._validate_connector_bootstrap_config(
        path, huggingface_enabled=True, expected_actor=expected_actor
    )


def test_huggingface_oauth_source_matches_closed_account_center_dto(tmp_path: Path) -> None:
    """Validate the private OAuth seed shape and target scope without exposing tokens."""

    path = tmp_path / "huggingface-oauth.json"
    document = {
        "schema_version": 1,
        "provider": "huggingface",
        "auth_mode": "device_oauth",
        "client_id": "public-client-id",
        "oauth_scopes": ["profile", "read-repos", "write-repos"],
        "access_token": "synthetic-access-token",
        "refresh_token": "synthetic-refresh-token",
        "access_token_expires_at": 1_900_000_000,
        "refresh_token_expires_at": None,
        "target_scope": {
            "repositories": [
                {
                    "repo_type": "model",
                    "repo_id": "owner/private-model",
                    "permissions": ["read", "write"],
                }
            ]
        },
        "credential_secret_ref": "huggingface/connector-token",
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    preflight._validate_huggingface_oauth_source(path)

    document["oauth_scopes"] = []
    path.write_text(json.dumps(document), encoding="utf-8")
    preflight._validate_huggingface_oauth_source(path)

    document["oauth_scopes"] = None
    path.write_text(json.dumps(document), encoding="utf-8")
    preflight._validate_huggingface_oauth_source(path)

    document["target_scope"]["repositories"][0]["permissions"] = ["read", "execute"]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="incomplete or out of scope"):
        preflight._validate_huggingface_oauth_source(path)

    document["target_scope"]["repositories"][0]["permissions"] = ["read", "write"]
    serialized = json.dumps(document).replace(
        '"client_id": "public-client-id"',
        '"client_id": "public-client-id", "client_id": "ambiguous"',
        1,
    )
    path.write_text(serialized, encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="invalid JSON"):
        preflight._validate_huggingface_oauth_source(path)


def test_compose_memory_limits_accept_docker_normalized_bytes() -> None:
    """Budget rendered Compose byte values rather than rejecting their schema form."""

    config = {
        "services": {
            "app": {"image": "test:local", "mem_limit": "104857600", "cpus": 0.2},
            "teams-public-ingress": {
                "image": "test:local",
                "mem_limit": "67108864",
                "cpus": 0.05,
                "ports": [
                    {
                        "target": 443,
                        "published": "18443",
                        "host_ip": "127.0.0.1",
                        "protocol": "tcp",
                    }
                ],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str((DEPLOYMENT_ROOT / "Caddyfile.staging").resolve()),
                        "target": "/etc/caddy/Caddyfile",
                    }
                ],
            },
        }
    }
    preflight._validate_compose(config, "stage1", 3072, 1.8, {"test:local"})


def test_stage3_cloud_provider_counts_toward_runtime_budget() -> None:
    """Keep the single RunPod-enabled Cloud Management process inside the host budget."""

    caddyfile = str((DEPLOYMENT_ROOT / "Caddyfile.staging").resolve())
    config = {
        "services": {
            "app": {"image": "test:local", "mem_limit": "100m", "cpus": "0.20"},
            "cloud-management-phase2": {
                "image": "test:local",
                "mem_limit": "900m",
                "cpus": "0.10",
                "profiles": ["cloud-phase2", "runpod"],
            },
            "teams-public-ingress": {
                "image": "test:local",
                "mem_limit": "64m",
                "cpus": "0.05",
                "ports": [
                    {
                        "target": 443,
                        "published": "18443",
                        "host_ip": "127.0.0.1",
                        "protocol": "tcp",
                    }
                ],
                "volumes": [
                    {"type": "bind", "source": caddyfile, "target": "/etc/caddy/Caddyfile"}
                ],
            },
        }
    }
    with pytest.raises(preflight.PreflightError, match="memory limits total"):
        preflight._validate_compose(config, "stage3", 1024, 1.8, {"test:local"})
