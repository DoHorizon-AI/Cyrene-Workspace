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
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
preflight = importlib.import_module("preflight")


DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]
UUID = "00000000-0000-4000-8000-000000000001"


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


def test_bootstrap_preflight_is_explicitly_not_full_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Return a distinct non-ready state for the empty-binding dependency bootstrap."""

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
    monkeypatch.setattr(
        preflight,
        "_compose_config",
        lambda *_args: {"services": {"exchange": {"image": "locked:test"}}},
    )
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
        cgroup_budget_mib=2432,
        minimum_available_mib=1024,
    )
    assert stage1["status"] == "READY_FOR_STAGING_MEASUREMENT"
    assert stage1["runtimeRssMeasurementRequired"] is True

    with pytest.raises(preflight.PreflightError, match="requires 1408 MiB"):
        preflight._memory_headroom(
            mode="stage2",
            config=config,
            available_mib=1407,
            cgroup_budget_mib=2816,
            minimum_available_mib=1024,
        )
    cutover = preflight._memory_headroom(
        mode="cutover",
        config=config,
        available_mib=1024,
        cgroup_budget_mib=2816,
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
        "platformAuthority": {"connectorWorkload": {"connectionRef": "grpcs://authority:7443"}},
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
