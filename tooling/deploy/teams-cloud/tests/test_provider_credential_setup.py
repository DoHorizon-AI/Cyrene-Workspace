"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_provider_credential_setup.py                                     │
│  Module: tooling.deploy.teams_cloud.tests.test_provider_credential_setup │
│  Role: Verify loopback auth setup, provider probes, and private writes.   │
│                                                                          │
│  模块职责：验证 loopback 授权页面、只读 Provider 探针和私有原子写入。    │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import re
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
setup = importlib.import_module("provider_credential_setup")

DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]


FAKE_ACCESS = "fake-access-token-never-print"
FAKE_REFRESH = "fake-refresh-token-never-print"
FAKE_RUNPOD_KEY = "fake-runpod-key-never-print"


@pytest.fixture
def private_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create synthetic owner-only artifacts without reading any operator state."""

    owner_uid = os.geteuid()
    monkeypatch.setattr(setup, "OWNER_UID", owner_uid)
    artifacts = tmp_path / ".artifacts" / "provider-setup"
    artifacts.mkdir(mode=0o700, parents=True)
    artifacts.chmod(0o700)
    secrets = artifacts / "secrets"
    secrets.mkdir(mode=0o700)
    secrets.chmod(0o700)
    return artifacts, secrets


def _seed_document(*, scopes: list[str] | None = None) -> dict[str, Any]:
    """Return a synthetic closed seed DTO for repository-selection tests."""

    return {
        "schema_version": 1,
        "provider": "huggingface",
        "auth_mode": "device_oauth",
        "client_id": setup.HF_CLIENT_ID,
        "oauth_scopes": scopes or ["openid", "profile", "read-repos"],
        "access_token": FAKE_ACCESS,
        "refresh_token": FAKE_REFRESH,
        "access_token_expires_at": 4_000_000_000,
        "refresh_token_expires_at": 4_000_000_000,
        "target_scope": {
            "repositories": [
                {
                    "repo_type": "model",
                    "repo_id": "Qwen/Qwen2.5-0.5B-Instruct",
                    "permissions": ["read"],
                }
            ]
        },
        "credential_secret_ref": setup.HF_SECRET_REF,
    }


def _write_active_runtime_env(artifacts: Path) -> Path:
    """Create a synthetic private env input with one registered HF profile."""

    integration = {
        "schemaVersion": 1,
        "platformAuthority": {
            "connectorWorkload": {"connectionRef": "grpcs://workspace-authority:7443"}
        },
        "accountCenter": {"connectionRef": "grpcs://account-center:7443"},
        "githubIssues": {"connectionRef": "grpcs://github-issues:7443"},
        "connectorHost": {"teamsMessages": {}},
        "authorizationCloud": {
            "profiles": {
                "hf-primary": {
                    "provider": "hf",
                    "directPlugin": {
                        "connectionRef": "grpcs://huggingface:7443",
                        "transport": "mtls",
                    },
                    "accountScopes": {
                        "repositories": [
                            {
                                "repoType": "model",
                                "repoId": "Qwen/Old-Model",
                                "permissions": ["read"],
                            }
                        ]
                    },
                }
            }
        },
    }
    provider = {"existingProviderSetting": "retained"}
    active_env = artifacts / "runtime.env"
    active_env.write_text(
        "# preserve this comment\n"
        "CYRENE_CONFIG_DIR=/old/private/config\n"
        "CYRENE_HF_OAUTH_BOOTSTRAP_FILE=/old/private/hf-seed.json\n"
        f"CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON={json.dumps(integration, separators=(',', ':'))}\n"
        f"CYRENE_HUGGINGFACE_CONFIGURATION_JSON={json.dumps(provider, separators=(',', ':'))}\n"
        "CYRENE_UNRELATED_CONFIG=untouched\n",
        encoding="utf-8",
    )
    active_env.chmod(0o600)
    return active_env


class FakeHub:
    """Return deterministic identity and repository metadata without network calls."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or [
            {
                "repoType": "model",
                "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                "private": False,
                "gated": False,
            }
        ]
        self.calls: list[tuple[str, ...]] = []

    def whoami(self, token: str) -> dict[str, Any]:
        self.calls.append(("whoami", token))
        return {"username": "Baijin64", "organizations": ["DoHorizon-AI"]}

    def repositories(
        self, token: str, repo_type: str, owner: str, search: str
    ) -> list[dict[str, Any]]:
        self.calls.append(("repositories", token, repo_type, owner, search))
        return [row for row in self.rows if row["repoType"] == repo_type]


def test_status_and_page_render_do_not_contact_provider_or_expose_imported_seed(
    private_dirs: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening the local page is offline and never renders stored token values."""

    artifacts, secrets = private_dirs
    seed_path = artifacts / "owner-seed.json"
    seed_path.write_text(json.dumps(_seed_document()), encoding="utf-8")
    seed_path.chmod(0o400)
    hub = FakeHub()
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=hub,
        runpod_probe=lambda _key: (200, {"pods": []}),
        imported_hf_seed=seed_path,
    )
    _session_id, session = service.create_session()

    state = service.status(session)
    page = setup._private_page().format(
        nonce="test-nonce",
        csrf="test-csrf",
        csrf_json='"test-csrf"',
    )

    assert state["huggingface"]["state"] == "READY_FOR_SELECTION"
    assert hub.calls == []
    assert FAKE_ACCESS not in page
    assert FAKE_REFRESH not in page
    assert "Qwen2.5-0.5B-Instruct" in page
    assert 'type="password"' in page


def test_hf_device_flow_search_and_selection_persists_only_a_scoped_seed(
    private_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Official device OAuth material stays server-side until selected repos are saved."""

    artifacts, secrets = private_dirs
    monkeypatch.setattr(
        setup,
        "_validate_huggingface_oauth_source",
        lambda path: setup._validate_seed(json.loads(path.read_text(encoding="utf-8"))),
    )
    oauth_requests: list[tuple[str, dict[str, str]]] = []
    poll_count = 0
    requested_scope = ""
    active_env = _write_active_runtime_env(artifacts)
    original_active_env = active_env.read_bytes()

    def oauth_transport(path: str, fields: dict[str, str]) -> tuple[int, dict[str, Any]]:
        nonlocal poll_count, requested_scope
        oauth_requests.append((path, dict(fields)))
        if path == "/oauth/device":
            requested_scope = fields["scope"]
            return 200, {
                "device_code": "fake-device-code-never-print",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://huggingface.co/oauth/device",
                "interval": 1,
                "expires_in": 120,
            }
        poll_count += 1
        if poll_count == 1:
            return 400, {"error": "authorization_pending"}
        return 200, {
            "access_token": FAKE_ACCESS,
            "refresh_token": FAKE_REFRESH,
            "expires_in": 3600,
            "refresh_token_expires_in": 86_400,
            "scope": requested_scope,
            "token_type": "Bearer",
        }

    hub = FakeHub()
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        oauth_transport=oauth_transport,
        hub_client=hub,
        runpod_probe=lambda _key: (200, {"pods": []}),
        active_env_file=active_env,
    )
    _session_id, session = service.create_session()
    started = service.start_hf_device_flow(
        session,
        write_access=False,
        include_gated=False,
    )

    assert started["userCode"] == "ABCD-EFGH"
    assert "fake-device-code-never-print" not in json.dumps(started)
    assert service.poll_hf_device_flow(session)["state"] == "AUTHORIZATION_PENDING"
    session.device_flow.last_poll_at = 0
    completed = service.poll_hf_device_flow(session)
    assert completed["state"] == "READY_FOR_SELECTION"
    assert completed["username"] == "Baijin64"
    assert "fake-device-code-never-print" not in json.dumps(completed)

    repos = service.list_hf_repositories(
        session, repo_type="model", owner="*", search="Qwen2.5-0.5B-Instruct"
    )
    saved = service.save_hf_selection(
        session,
        [
            {
                "repoType": "model",
                "repoId": repos["repositories"][0]["repoId"],
                "permissions": ["read"],
            }
        ],
    )

    output = Path(saved["candidateHuggingFaceSeed"])
    candidate_env = Path(saved["candidateEnvFile"])
    seed = json.loads(output.read_text(encoding="utf-8"))
    candidate_text = candidate_env.read_text(encoding="utf-8")
    candidate_values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in candidate_text.splitlines()
        if "=" in line and line.startswith("CYRENE_")
    }
    exchange_candidate = json.loads(
        candidate_values["CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"]
    )
    provider_candidate = json.loads(candidate_values["CYRENE_HUGGINGFACE_CONFIGURATION_JSON"])
    expected_scope = [
        {
            "repo_type": "model",
            "repo_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "permissions": ["read"],
        }
    ]
    assert saved["state"] == "SAVED_CANDIDATE_REQUIRES_PREPARATION"
    assert saved["activation"] == "FORMAL_STAGE_TRANSITION_REQUIRED"
    candidate_config_dir = Path(saved["candidateConfigDir"])
    assert candidate_env.parent == output.parent
    assert candidate_env.parent.stat().st_uid == setup.OWNER_UID
    assert stat.S_IMODE(candidate_env.parent.stat().st_mode) == 0o700
    assert candidate_config_dir == candidate_env.parent / "config"
    assert candidate_config_dir.is_dir() and not list(candidate_config_dir.iterdir())
    assert candidate_config_dir.stat().st_uid == setup.OWNER_UID
    assert stat.S_IMODE(candidate_config_dir.stat().st_mode) == 0o700
    assert candidate_env.stat().st_uid == setup.OWNER_UID
    assert stat.S_IMODE(candidate_env.stat().st_mode) == 0o600
    assert output.stat().st_uid == setup.OWNER_UID
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    assert candidate_env.stat().st_mode & 0o077 == 0
    assert saved["candidateEnvSha256"] == hashlib.sha256(candidate_env.read_bytes()).hexdigest()
    assert seed["target_scope"]["repositories"] == [*expected_scope]
    hf_profile = exchange_candidate["authorizationCloud"]["profiles"]["hf-primary"]
    assert hf_profile["accountScopes"] == {
        "repositories": [
            {
                "repoType": "model",
                "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                "permissions": ["read"],
            }
        ]
    }
    assert provider_candidate["repositories"] == expected_scope
    assert provider_candidate["read_only"] is True
    assert provider_candidate["existingProviderSetting"] == "retained"
    assert candidate_values["CYRENE_CONFIG_DIR"] == str(candidate_config_dir)
    assert candidate_values["CYRENE_HF_OAUTH_BOOTSTRAP_FILE"] == str(output)
    assert active_env.read_bytes() == original_active_env
    assert FAKE_ACCESS not in candidate_text
    assert FAKE_REFRESH not in candidate_text
    assert oauth_requests[0][1]["scope"] == "openid profile read-repos"
    assert FAKE_ACCESS not in json.dumps(started)
    assert FAKE_REFRESH not in json.dumps(completed)
    assert FAKE_ACCESS not in json.dumps(saved)
    assert FAKE_REFRESH not in json.dumps(saved)


@pytest.mark.parametrize(
    "expected_error",
    [
        "huggingface_exchange_profile_unavailable",
        "huggingface_exchange_profile_invalid",
        "exchange_configuration_invalid",
    ],
)
def test_hf_candidate_rejects_unregistered_or_ambiguous_source_without_publishing(
    private_dirs: tuple[Path, Path],
    expected_error: str,
) -> None:
    """A candidate is never published from an unknown profile or ambiguous env value."""

    artifacts, secrets = private_dirs
    active_env = _write_active_runtime_env(artifacts)
    original = active_env.read_text(encoding="utf-8")
    if expected_error in {
        "huggingface_exchange_profile_unavailable",
        "huggingface_exchange_profile_invalid",
    }:
        invalid_profile = (
            {"provider": "gcp", "directPlugin": {"connectionRef": "grpcs://huggingface:7443"}}
            if expected_error == "huggingface_exchange_profile_unavailable"
            else {
                "provider": "hf",
                "directPlugin": {
                    "connectionRef": "grpcs://huggingface:7443",
                    "transport": "mtls",
                },
                "accountScopes": {"repositories": [], "extra": "unsupported"},
            }
        )
        bad_exchange = json.dumps(
            {"authorizationCloud": {"profiles": {"hf-primary": invalid_profile}}},
            separators=(",", ":"),
        )
        active_env.write_text(
            original.replace(
                next(
                    line.split("=", 1)[1]
                    for line in original.splitlines()
                    if line.startswith("CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON=")
                ),
                bad_exchange,
            ),
            encoding="utf-8",
        )
    else:
        lines = original.splitlines()
        line = next(
            row
            for row in lines
            if row.startswith("CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON=")
        )
        key, _value = line.split("=", 1)
        lines[lines.index(line)] = (
            f'{key}={{"authorizationCloud":{{"profiles":{{"hf-primary":{{"provider":"hf"}}}}}},"authorizationCloud":{{}}}}'
        )
        active_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    active_env.chmod(0o600)

    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=FakeHub(),
        runpod_probe=lambda _key: (200, {"pods": []}),
        active_env_file=active_env,
    )
    session = service.create_session()[1]
    session.seed = _seed_document()
    session.identity = {"username": "Baijin64", "organizations": ["DoHorizon-AI"]}
    service.list_hf_repositories(
        session, repo_type="model", owner="*", search="Qwen2.5-0.5B-Instruct"
    )

    with pytest.raises(setup.SetupError, match=expected_error):
        service.save_hf_selection(
            session,
            [
                {
                    "repoType": "model",
                    "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                    "permissions": ["read"],
                }
            ],
        )

    candidates = artifacts / "onboarding" / "private" / "provider-candidates"
    assert not candidates.exists() or not list(candidates.iterdir())


def test_hf_candidate_rejects_explicit_null_exchange_account_scopes(
    private_dirs: tuple[Path, Path],
) -> None:
    """An explicit null scope is invalid and is not silently normalized as missing."""

    artifacts, secrets = private_dirs
    active_env = _write_active_runtime_env(artifacts)
    content = active_env.read_text(encoding="utf-8")
    lines = content.splitlines()
    exchange_line = next(
        line for line in lines if line.startswith("CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON=")
    )
    name, encoded = exchange_line.split("=", 1)
    exchange = json.loads(encoded)
    exchange["authorizationCloud"]["profiles"]["hf-primary"]["accountScopes"] = None
    lines[lines.index(exchange_line)] = f"{name}={json.dumps(exchange, separators=(',', ':'))}"
    active_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    active_env.chmod(0o600)
    hub = FakeHub()
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=hub,
        active_env_file=active_env,
    )
    session = service.create_session()[1]
    session.seed = _seed_document()
    session.identity = hub.whoami(FAKE_ACCESS)
    service.list_hf_repositories(
        session, repo_type="model", owner="*", search="Qwen2.5-0.5B-Instruct"
    )

    with pytest.raises(setup.SetupError, match="huggingface_exchange_profile_invalid"):
        service.save_hf_selection(
            session,
            [
                {
                    "repoType": "model",
                    "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                    "permissions": ["read"],
                }
            ],
        )
    candidates = artifacts / "onboarding" / "private" / "provider-candidates"
    assert not candidates.exists() or not list(candidates.iterdir())


def test_hf_candidate_write_permission_enables_only_matching_provider_scope(
    private_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write grant is explicit in OAuth, Exchange, and provider config together."""

    artifacts, secrets = private_dirs
    monkeypatch.setattr(
        setup,
        "_validate_huggingface_oauth_source",
        lambda path: setup._validate_seed(json.loads(path.read_text(encoding="utf-8"))),
    )
    active_env = _write_active_runtime_env(artifacts)
    seed = _seed_document(scopes=["openid", "profile", "read-repos", "write-repos"])
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=FakeHub(),
        active_env_file=active_env,
    )
    session = service.create_session()[1]
    session.seed = seed
    session.identity = {"username": "Baijin64", "organizations": ["DoHorizon-AI"]}
    service.list_hf_repositories(
        session, repo_type="model", owner="*", search="Qwen2.5-0.5B-Instruct"
    )

    result = service.save_hf_selection(
        session,
        [
            {
                "repoType": "model",
                "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                "permissions": ["read", "write"],
            }
        ],
    )
    env_lines = Path(result["candidateEnvFile"]).read_text(encoding="utf-8").splitlines()
    values = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in env_lines if "=" in line}
    provider = json.loads(values["CYRENE_HUGGINGFACE_CONFIGURATION_JSON"])
    exchange = json.loads(values["CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON"])
    row = {
        "repo_type": "model",
        "repo_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "permissions": ["read", "write"],
    }
    assert result["accessMode"] == "read_write"
    assert provider["read_only"] is False
    assert provider["repositories"] == [row]
    assert exchange["authorizationCloud"]["profiles"]["hf-primary"]["accountScopes"][
        "repositories"
    ] == [
        {
            "repoType": "model",
            "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
            "permissions": ["read", "write"],
        }
    ]


def test_hf_save_requires_private_active_env_input_and_reports_candidate_readiness(
    private_dirs: tuple[Path, Path],
) -> None:
    """The page cannot fall back to writing an unpaired standalone OAuth seed."""

    artifacts, secrets = private_dirs
    hub = FakeHub()
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=hub,
        runpod_probe=lambda _key: (200, {"pods": []}),
    )
    session = service.create_session()[1]
    session.seed = _seed_document()
    session.identity = hub.whoami(FAKE_ACCESS)
    service.list_hf_repositories(
        session, repo_type="model", owner="*", search="Qwen2.5-0.5B-Instruct"
    )
    assert service.status(session)["huggingfaceCandidateInput"] == "RUNTIME_ENV_FILE_REQUIRED"

    with pytest.raises(setup.SetupError, match="runtime_env_required_for_huggingface_candidate"):
        service.save_hf_selection(
            session,
            [
                {
                    "repoType": "model",
                    "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                    "permissions": ["read"],
                }
            ],
        )
    assert not (artifacts / "onboarding" / "private" / "provider-candidates").exists()


def test_hf_candidate_rejects_duplicate_runtime_env_assignments(
    private_dirs: tuple[Path, Path],
) -> None:
    """A candidate is not generated from a Compose env file with ambiguous assignments."""

    artifacts, secrets = private_dirs
    active_env = _write_active_runtime_env(artifacts)
    content = active_env.read_text(encoding="utf-8")
    duplicate = next(
        line
        for line in content.splitlines()
        if line.startswith("CYRENE_HUGGINGFACE_CONFIGURATION_JSON=")
    )
    active_env.write_text(content + duplicate + "\n", encoding="utf-8")
    active_env.chmod(0o600)
    hub = FakeHub()
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=hub,
        active_env_file=active_env,
    )
    session = service.create_session()[1]
    session.seed = _seed_document()
    session.identity = hub.whoami(FAKE_ACCESS)
    service.list_hf_repositories(
        session, repo_type="model", owner="*", search="Qwen2.5-0.5B-Instruct"
    )

    with pytest.raises(setup.SetupError, match="runtime_env_assignment_invalid"):
        service.save_hf_selection(
            session,
            [
                {
                    "repoType": "model",
                    "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                    "permissions": ["read"],
                }
            ],
        )
    candidates = artifacts / "onboarding" / "private" / "provider-candidates"
    assert not candidates.exists() or not list(candidates.iterdir())


@pytest.mark.parametrize("unsafe_input", ["group_readable", "symlink"])
def test_hf_candidate_rejects_non_private_active_env(
    private_dirs: tuple[Path, Path], unsafe_input: str
) -> None:
    """An active env source must remain a private regular file beneath artifacts."""

    artifacts, secrets = private_dirs
    active_env = _write_active_runtime_env(artifacts)
    if unsafe_input == "group_readable":
        active_env.chmod(0o640)
    else:
        alternate = artifacts / "runtime-env-source"
        active_env.rename(alternate)
        active_env.symlink_to(alternate)
    hub = FakeHub()
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=hub,
        active_env_file=active_env,
    )
    session = service.create_session()[1]
    session.seed = _seed_document()
    session.identity = hub.whoami(FAKE_ACCESS)
    service.list_hf_repositories(
        session, repo_type="model", owner="*", search="Qwen2.5-0.5B-Instruct"
    )

    with pytest.raises(setup.SetupError, match="runtime_env_permissions_invalid"):
        service.save_hf_selection(
            session,
            [
                {
                    "repoType": "model",
                    "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                    "permissions": ["read"],
                }
            ],
        )
    candidates = artifacts / "onboarding" / "private" / "provider-candidates"
    assert not candidates.exists() or not list(candidates.iterdir())


def test_hf_selection_rejects_unlisted_and_ungranted_writes_without_creating_seed(
    private_dirs: tuple[Path, Path],
) -> None:
    """A browser cannot widen the repository cache or OAuth permissions by editing JSON."""

    artifacts, secrets = private_dirs
    hub = FakeHub()
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=hub,
        runpod_probe=lambda _key: (200, {"pods": []}),
    )
    _session_id, session = service.create_session()
    session.seed = _seed_document()
    session.identity = hub.whoami(FAKE_ACCESS)
    service.list_hf_repositories(session, repo_type="model", owner="*", search="Qwen")

    with pytest.raises(setup.SetupError, match="selection_out_of_scope"):
        service.save_hf_selection(
            session,
            [
                {
                    "repoType": "model",
                    "repoId": "Qwen/Not-Returned-By-Search",
                    "permissions": ["read"],
                }
            ],
        )
    with pytest.raises(setup.SetupError, match="selection_out_of_scope"):
        service.save_hf_selection(
            session,
            [
                {
                    "repoType": "model",
                    "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                    "permissions": ["read", "write"],
                }
            ],
        )
    assert not (artifacts / "onboarding" / "private" / "huggingface-oauth.json").exists()


def test_runpod_probe_saves_only_after_success_and_never_returns_key(
    private_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid candidate is checked by GET Pods and atomically saved outside active secrets."""

    artifacts, secrets = private_dirs
    probes: list[str] = []
    active_key = secrets / "runpod-api-key"
    active_key.write_text("existing-active-key", encoding="ascii")
    active_key.chmod(0o400)
    monkeypatch.setattr(setup, "_secret_file", lambda *_args: None)
    active_env = _write_active_runtime_env(artifacts)

    def probe(api_key: str) -> tuple[int, Any]:
        probes.append(api_key)
        return 200, {"pods": [], "pagination": {"hasNextPage": False}}

    service = setup.CredentialSetupService(
        artifacts, secrets, hub_client=FakeHub(), runpod_probe=probe, active_env_file=active_env
    )
    session = service.create_session()[1]
    result = service.save_runpod_key(session, FAKE_RUNPOD_KEY)
    path = Path(result["candidateRunpodKeyFile"])
    env_path = Path(result["candidateEnvFile"])

    assert probes == [FAKE_RUNPOD_KEY]
    assert result["state"] == "SAVED_CANDIDATE_REQUIRES_PREPARATION"
    assert result["activation"] == "FORMAL_STAGE_TRANSITION_REQUIRED"
    assert path.read_text(encoding="ascii") == FAKE_RUNPOD_KEY
    assert path.stat().st_uid == setup.OWNER_UID
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert env_path.parent.stat().st_uid == setup.OWNER_UID
    assert stat.S_IMODE(env_path.parent.stat().st_mode) == 0o700
    candidate_config_dir = Path(result["candidateConfigDir"])
    assert candidate_config_dir == env_path.parent / "config"
    assert candidate_config_dir.is_dir() and not list(candidate_config_dir.iterdir())
    env_values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    }
    assert env_values["CYRENE_RUNPOD_API_KEY_FILE"] == str(path)
    assert env_values["CYRENE_CONFIG_DIR"] == str(candidate_config_dir)
    assert active_key.read_text(encoding="ascii") == "existing-active-key"
    assert FAKE_RUNPOD_KEY not in json.dumps(result)
    assert service.status(session)["runpod"]["candidateRunpodKeyFile"] == str(path)


def test_runpod_candidate_requires_an_active_env_input_before_provider_probe(
    private_dirs: tuple[Path, Path],
) -> None:
    """Do not validate a key when there is no complete activation candidate to produce."""

    artifacts, secrets = private_dirs
    probes: list[str] = []
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=FakeHub(),
        runpod_probe=lambda key: (probes.append(key) or 200, {"pods": []}),
    )
    session = service.create_session()[1]

    with pytest.raises(setup.SetupError, match="runtime_env_required_for_runpod_candidate"):
        service.save_runpod_key(session, FAKE_RUNPOD_KEY)

    assert probes == []
    assert not (artifacts / "onboarding" / "private" / "provider-candidates").exists()


def test_candidate_writers_reject_root_before_creating_owner_only_outputs(
    private_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper is UID-10001-only and does not publish root-owned config children."""

    artifacts, _secrets = private_dirs
    active_env = _write_active_runtime_env(artifacts)
    monkeypatch.setattr(setup, "OWNER_UID", 10001)
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)

    with pytest.raises(setup.SetupError, match="private_output_owner_invalid"):
        setup._write_hf_candidate_bundle(artifacts, active_env, _seed_document())
    with pytest.raises(setup.SetupError, match="private_output_owner_invalid"):
        setup._write_runpod_candidate_bundle(artifacts, active_env, FAKE_RUNPOD_KEY)

    assert not (artifacts / "onboarding" / "private" / "provider-candidates").exists()


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (401, "runpod_authentication_failed"),
        (403, "runpod_pods_read_permission_denied"),
        (429, "runpod_rate_limited"),
        (200, "runpod_validation_failed"),
    ],
)
def test_runpod_failure_does_not_replace_existing_key(
    private_dirs: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_error: str,
) -> None:
    """Authentication, permission, rate-limit, and malformed responses preserve the old file."""

    artifacts, secrets = private_dirs
    path = secrets / "runpod-api-key"
    path.write_text("old-valid-key", encoding="ascii")
    path.chmod(0o400)
    active_env = _write_active_runtime_env(artifacts)
    monkeypatch.setattr(setup, "_secret_file", lambda *_args: None)
    payload = {} if status == 200 else None
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=FakeHub(),
        runpod_probe=lambda _key: (status, payload),
        active_env_file=active_env,
    )
    session = service.create_session()[1]

    with pytest.raises(setup.SetupError, match=expected_error):
        service.save_runpod_key(session, FAKE_RUNPOD_KEY)

    assert path.read_text(encoding="ascii") == "old-valid-key"


def test_local_page_enforces_exact_host_origin_csrf_and_redacts_api_key(
    private_dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local browser boundary rejects DNS-rebinding/CSRF and never echoes a key."""

    artifacts, secrets = private_dirs
    calls: list[str] = []
    monkeypatch.setattr(setup, "_secret_file", lambda *_args: None)
    active_env = _write_active_runtime_env(artifacts)
    service = setup.CredentialSetupService(
        artifacts,
        secrets,
        hub_client=FakeHub(),
        runpod_probe=lambda key: (calls.append(key) or 200, {"pods": []}),
        active_env_file=active_env,
    )
    server = setup.ProviderSetupServer(("127.0.0.1", 0), service, server_nonce="fixed-test-nonce")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    origin = f"http://127.0.0.1:{port}"
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        cookie = response.getheader("Set-Cookie").split(";", maxsplit=1)[0]
        csp = response.getheader("Content-Security-Policy")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        assert response.status == 200
        assert "connect-src 'self'" in csp
        assert "Qwen2.5-0.5B-Instruct" in page

        connection.request("GET", "/status", headers={"Cookie": cookie})
        same_origin = connection.getresponse()
        same_origin_status = json.loads(same_origin.read().decode("utf-8"))
        assert same_origin.status == 200
        assert same_origin_status["huggingfaceCandidateInput"] == "AVAILABLE"

        body = json.dumps({"csrf": csrf, "api_key": FAKE_RUNPOD_KEY})
        connection.request(
            "POST",
            "/runpod",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": "http://attacker.invalid",
                "X-Cyrene-CSRF": csrf,
            },
        )
        denied = connection.getresponse()
        denied_body = denied.read().decode("utf-8")
        assert denied.status == 403
        assert calls == []
        assert FAKE_RUNPOD_KEY not in denied_body
        assert not (secrets / "runpod-api-key").exists()

        connection.request(
            "POST",
            "/runpod",
            body=json.dumps({"csrf": csrf, "api_key": FAKE_RUNPOD_KEY}),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": origin,
                "X-Cyrene-CSRF": "wrong-token",
            },
        )
        csrf_denied = connection.getresponse()
        assert csrf_denied.status == 403
        csrf_denied_body = csrf_denied.read().decode("utf-8")
        assert FAKE_RUNPOD_KEY not in csrf_denied_body
        assert calls == []

        connection.close()
        other = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        other.request(
            "POST",
            "/runpod",
            body=json.dumps({"csrf": csrf, "api_key": FAKE_RUNPOD_KEY}),
            headers={
                "Host": f"localhost:{port}",
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": origin,
                "X-Cyrene-CSRF": csrf,
            },
        )
        host_denied = other.getresponse()
        assert host_denied.status == 403
        assert FAKE_RUNPOD_KEY not in host_denied.read().decode("utf-8")
        assert calls == []
        other.close()

        valid = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        valid.request(
            "POST",
            "/runpod",
            body=json.dumps({"csrf": csrf, "api_key": FAKE_RUNPOD_KEY}),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": origin,
                "X-Cyrene-CSRF": csrf,
            },
        )
        accepted = valid.getresponse()
        result = accepted.read().decode("utf-8")
        assert accepted.status == 200
        assert "SAVED_CANDIDATE_REQUIRES_PREPARATION" in result
        assert str(secrets / "runpod-api-key") not in result
        assert FAKE_RUNPOD_KEY not in result
        assert calls == [FAKE_RUNPOD_KEY]
        valid.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_credential_notice_overlay_is_optional_private_and_owner_scoped() -> None:
    """The notice profile adds only its private config mount and no new service or token."""

    compose = yaml.safe_load(
        (DEPLOYMENT_ROOT / "compose.credential-notifications.yaml").read_text(encoding="utf-8")
    )
    service = compose["services"]["navigator"]
    assert set(compose["services"]) == {"navigator"}
    assert "ports" not in service
    assert service["environment"] == {
        "TEAMS_CREDENTIAL_NOTIFICATIONS_CONFIG_FILE": "/run/secrets/teams-credential-notifications.json"
    }
    assert service["configs"] == [
        {
            "source": "teams-credential-notifications",
            "target": "/run/secrets/teams-credential-notifications.json",
        }
    ]
    assert compose["configs"]["teams-credential-notifications"]["file"].endswith(
        "/teams-credential-notifications.json"
    )

    template = json.loads(
        (DEPLOYMENT_ROOT / "config" / "teams-credential-notifications.template.json").read_text(
            encoding="utf-8"
        )
    )
    schema = json.loads(
        (DEPLOYMENT_ROOT / "config" / "teams-credential-notifications.schema.json").read_text(
            encoding="utf-8"
        )
    )
    exchange_template = json.loads(
        (
            DEPLOYMENT_ROOT
            / "config"
            / "teams-credential-notifications-exchange-profile.template.json"
        ).read_text(encoding="utf-8")
    )
    exchange_schema = json.loads(
        (
            DEPLOYMENT_ROOT
            / "config"
            / "teams-credential-notifications-exchange-profile.schema.json"
        ).read_text(encoding="utf-8")
    )
    principal = json.loads(
        (DEPLOYMENT_ROOT / "config" / "navigator-principal-config.template.json").read_text(
            encoding="utf-8"
        )
    )
    assert template["schemaVersion"] == 1
    assert template["connections"] == []
    assert template["owner"]["actorAadObjectId"] == ""
    assert schema["properties"]["owner"]["required"] == [
        "actorAadObjectId",
        "issuer",
        "subject",
        "tenantId",
    ]
    assert schema["properties"]["connections"]["items"]["properties"]["provider"]["enum"] == [
        "hf",
        "azure",
        "gcp",
        "runpod",
        "microsoft_graph",
    ]
    destination_properties = schema["properties"]["destination"]["properties"]
    assert destination_properties["rootThreadId"]["pattern"] == "^0*[1-9][0-9]*$"
    assert destination_properties["serviceUrl"]["maxLength"] == 512
    assert schema["properties"]["pollIntervalMs"]["maximum"] == 900000
    assert re.fullmatch(destination_properties["rootThreadId"]["pattern"], "0001792992696303")
    assert re.fullmatch(
        destination_properties["serviceUrl"]["pattern"],
        "https://smba.trafficmanager.net/amer/",
    )
    assert not re.fullmatch(
        destination_properties["serviceUrl"]["pattern"], "https://example.invalid/"
    )
    assert template["exchange"] == {"profileRef": ""}
    assert set(exchange_template["systemNotification"]) == {
        "profileRef",
        "ownerSubject",
        "fixedRoute",
    }
    assert exchange_schema["properties"]["systemNotification"]["additionalProperties"] is False
    assert principal["principals"][0]["token_env"] == "NAVIGATOR_SESSION_TOKEN"
    assert principal["principals"][0]["can_publish_credential_health"] is False
