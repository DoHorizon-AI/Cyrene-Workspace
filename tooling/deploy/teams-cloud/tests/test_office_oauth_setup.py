"""Exercise the private Microsoft Office OAuth setup boundary and fixed CLI bridge."""

from __future__ import annotations

import hmac
import http.client
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

DEPLOYMENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOYMENT_ROOT))
setup = importlib.import_module("office_oauth_setup")
release_bundle = importlib.import_module("release_bundle")

TENANT = "11111111-1111-4111-8111-111111111111"
SUBJECT = "22222222-2222-4222-8222-222222222222"
ORGANIZATION = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
EXTERNAL_SUBJECT = f"{TENANT}:{SUBJECT}"
PROFILE_REF = "profile-office-owner"
LINK_REF = "link-office-owner"
SCOPES = [
    "openid",
    "profile",
    "User.Read",
    "offline_access",
    "Tasks.ReadWrite",
    "Calendars.ReadWrite",
    "Sites.Selected",
]
ENABLED_FEATURES = {
    "plannerEnabled": True,
    "todoEnabled": True,
    "calendarEnabled": True,
    "mailMetadataEnabled": False,
    "mailDraftsEnabled": False,
    "mailSendEnabled": False,
    "sharePointDriveMetadataEnabled": True,
}
EXCHANGE_PROFILE_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,299}$")


def mail_features(
    *, metadata: bool = False, drafts: bool = False, send: bool = False
) -> dict[str, bool]:
    """Copy the non-mail profile and enable only explicitly selected Mail capabilities."""

    features = dict(ENABLED_FEATURES)
    features.update(
        {
            "mailMetadataEnabled": metadata,
            "mailDraftsEnabled": drafts,
            "mailSendEnabled": send,
        }
    )
    return features


def status_document(
    *,
    state: str = "NOT_LINKED",
    auth_status: str = "unknown",
    external_subject: str | None = None,
    revision: int | None = None,
    target_scope: dict[str, Any] | None = None,
    reauth_required: bool = False,
    scopes: list[str] | None = None,
    enabled_features: dict[str, bool] | None = None,
    target_ref_count: int = 0,
    mail_authorization_state: str | None = None,
) -> dict[str, Any]:
    """Build the exact safe status projection emitted by the fixed Exchange CLI."""

    document = {
        "state": state,
        "profileRef": PROFILE_REF,
        "provider": "microsoft_graph",
        "authStatus": auth_status,
        "accessMode": "actor_owned",
        "externalSubject": external_subject,
        "scopeSchemaVersion": 2,
        "targetScope": target_scope,
        "requiredOAuthScopes": list(SCOPES if scopes is None else scopes),
        "credentialRevision": revision,
        "credentialExpiresAt": None,
        "refreshExpiresAt": None,
        "reauthRequired": reauth_required,
        "reauthReasonCode": None,
        "enabledFeatures": dict(ENABLED_FEATURES if enabled_features is None else enabled_features),
        "targetRefCount": target_ref_count,
    }
    if mail_authorization_state is not None:
        document["mailAuthorizationState"] = mail_authorization_state
    return document


def scopes_for_features(features: dict[str, bool]) -> list[str]:
    """Return the small delegated scope set expected for the selected Office flags."""

    scopes = list(SCOPES)
    if features["mailDraftsEnabled"]:
        scopes.append("Mail.ReadWrite")
    elif features["mailMetadataEnabled"] or features["mailSendEnabled"]:
        scopes.append("Mail.ReadBasic")
    if features["mailSendEnabled"]:
        scopes.append("Mail.Send")
    return scopes


def operator_context() -> setup.OperatorContext:
    """Build private owner pins that never appear in a browser response."""

    return setup.OperatorContext(
        issuer=f"https://login.microsoftonline.com/{TENANT}/v2.0",
        tenant_id=TENANT,
        subject=SUBJECT,
        organization_id=ORGANIZATION,
        workspace_id=WORKSPACE,
        binding_ref="microsoft-graph.office-owner",
        profile_idempotency_key="office-owner-test-link",
    )


def authorization_url(
    port: int,
    *,
    scopes: str | None = None,
    authority: str = "organizations",
    state: str = "state-secret-123",
) -> str:
    """Return a test Microsoft authorize URL with the one registered loopback URI."""

    query = urllib.parse.urlencode(
        {
            "client_id": "office-test-client",
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": f"http://localhost:{port}{setup.CALLBACK_PATH}",
            "scope": scopes
            or "openid profile User.Read offline_access Tasks.ReadWrite Calendars.ReadWrite Sites.Selected",
            "state": state,
            "code_challenge": "A" * 43,
            "code_challenge_method": "S256",
        }
    )
    return f"https://login.microsoftonline.com/{authority}/oauth2/v2.0/authorize?{query}"


class FakeBridge:
    """Provide fixed Exchange CLI responses and retain only test-call evidence."""

    def __init__(self, port: int = setup.DEFAULT_PORT) -> None:
        self.port = port
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.begin_calls = 0
        self.expires_at = "2099-01-01T00:00:00Z"
        self.linked = False
        self.flow_kind_override: str | None = None
        self.omit_flow_kind = False
        self.status_value = status_document()
        self.discoveries: dict[str, dict[str, Any]] = {
            "planner_plans": {
                "resourceType": "planner_plans",
                "items": [{"id": "plan-1", "displayName": "Operations", "kind": "plan"}],
            },
            "todo_lists": {
                "resourceType": "todo_lists",
                "items": [{"id": "todo-1", "displayName": "My tasks", "kind": "task_list"}],
            },
            "calendars": {
                "resourceType": "calendars",
                "items": [{"id": "calendar-1", "displayName": "Calendar", "kind": "calendar"}],
            },
            "sharepoint_drives": {
                "resourceType": "sharepoint_drives",
                "items": [
                    {
                        "id": "drive-1",
                        "displayName": "Documents",
                        "driveType": "documentLibrary",
                        "siteId": "site-1",
                    }
                ],
            },
            "freebusy_subjects": {
                "resourceType": "freebusy_subjects",
                "items": [
                    {"id": "person@dohorizon.com", "displayName": "Pilot member", "kind": "person"}
                ],
            },
            "mail_folders": {
                "resourceType": "mail_folders",
                "items": [{"id": "mail-folder-1", "displayName": "Inbox", "kind": "mail_folder"}],
            },
        }

    def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, dict(payload)))
        if operation in {"begin", "begin-mail-scope-upgrade"}:
            self.begin_calls += 1
            flow_kind = self.flow_kind_override or (
                "scope_upgrade" if operation == "begin-mail-scope-upgrade" else "link"
            )
            scopes = " ".join(scopes_for_features(self.status_value["enabledFeatures"]))
            response = {
                "state": "AUTHORIZATION_REQUIRED",
                "provider": "microsoft_graph",
                "profileRef": PROFILE_REF,
                "linkSessionRef": LINK_REF,
                "authorizationUrl": authorization_url(
                    self.port,
                    scopes=scopes,
                    state=(
                        "state-secret-123"
                        if self.begin_calls == 1
                        else f"state-secret-{self.begin_calls}"
                    ),
                ),
                "expiresAt": self.expires_at,
            }
            if not self.omit_flow_kind:
                response["flowKind"] = flow_kind
            return response
        if operation == "status":
            return dict(self.status_value)
        if operation in {"complete", "complete-scope-upgrade"}:
            self.linked = True
            enabled_features = dict(self.status_value["enabledFeatures"])
            scopes = scopes_for_features(enabled_features)
            state = (
                "READY"
                if any(
                    enabled_features[name]
                    for name in ("mailMetadataEnabled", "mailDraftsEnabled", "mailSendEnabled")
                )
                else "DISABLED"
            )
            revision = (
                0
                if operation == "complete"
                else int(self.status_value["credentialRevision"] or 0) + 1
            )
            self.status_value = status_document(
                state="LINKED",
                auth_status="healthy",
                external_subject=EXTERNAL_SUBJECT,
                revision=revision,
                target_scope={"workspaceId": WORKSPACE},
                scopes=scopes,
                enabled_features=enabled_features,
                mail_authorization_state=state,
            )
            return dict(self.status_value)
        if operation == "discover":
            document = dict(self.discoveries[payload["resourceType"]])
            document.update(
                {
                    "state": "DISCOVERED",
                    "pageCursor": payload.get("pageCursor"),
                    "nextCursor": None,
                    "complete": True,
                    "truncated": False,
                    "credentialRevision": self.status_value["credentialRevision"],
                }
            )
            return document
        if operation == "select-targets":
            revision = int(payload["expectedCredentialRevision"]) + 1
            self.status_value = status_document(
                state="UPDATED",
                auth_status="healthy",
                external_subject=EXTERNAL_SUBJECT,
                revision=revision,
                target_scope={"workspaceId": WORKSPACE, **payload["targets"]},
                scopes=list(self.status_value["requiredOAuthScopes"]),
                enabled_features=dict(self.status_value["enabledFeatures"]),
                target_ref_count=sum(len(rows) for rows in payload["targets"].values()),
                mail_authorization_state=self.status_value.get(
                    "mailAuthorizationState", "DISABLED"
                ),
            )
            return dict(self.status_value)
        raise AssertionError(f"unexpected operation: {operation}")


class BlockingStatusBridge(FakeBridge):
    """Hold one status operation to exercise poll admission and HTTP liveness."""

    def __init__(self, port: int = setup.DEFAULT_PORT) -> None:
        super().__init__(port)
        self.status_started = threading.Event()
        self.release_status = threading.Event()

    def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "status":
            self.status_started.set()
            if not self.release_status.wait(timeout=5):
                raise AssertionError("test did not release blocked status bridge")
        return super().invoke(operation, payload)


class BlockingBeginBridge(FakeBridge):
    """Hold provider begin while repeated same-session submissions arrive."""

    def __init__(self, port: int = setup.DEFAULT_PORT) -> None:
        super().__init__(port)
        self.begin_started = threading.Event()
        self.release_begin = threading.Event()

    def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "begin":
            self.begin_started.set()
            if not self.release_begin.wait(timeout=5):
                raise AssertionError("test did not release blocked begin bridge")
        return super().invoke(operation, payload)


class BlockingStatusBeginBridge(FakeBridge):
    """Hold a poll then begin to verify the status message advances between them."""

    def __init__(self, port: int = setup.DEFAULT_PORT) -> None:
        super().__init__(port)
        self.status_started = threading.Event()
        self.release_status = threading.Event()
        self.begin_started = threading.Event()
        self.release_begin = threading.Event()

    def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "status":
            self.status_started.set()
            if not self.release_status.wait(timeout=5):
                raise AssertionError("test did not release blocked status bridge")
        elif operation == "begin":
            self.begin_started.set()
            if not self.release_begin.wait(timeout=5):
                raise AssertionError("test did not release blocked begin bridge")
        return super().invoke(operation, payload)


@pytest.fixture
def linked_service() -> tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession]:
    """Create an owner profile fixture whose identity was checked by Exchange."""

    bridge = FakeBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
    )
    return service, bridge, session


def test_operator_template_matches_exact_private_schema_shape() -> None:
    """Keep operator identity pins separate from provider tokens, scopes, and targets."""

    schema = json.loads((DEPLOYMENT_ROOT / "config/office-operator.schema.json").read_text())
    template = json.loads((DEPLOYMENT_ROOT / "config/office-operator.template.json").read_text())
    assert schema["required"] == ["schemaVersion", "actor", "bindingRef", "profileIdempotencyKey"]
    assert schema["additionalProperties"] is False
    assert set(template) == {"schemaVersion", "actor", "bindingRef", "profileIdempotencyKey"}
    assert set(template["actor"]) == {"issuer", "subject", "organizationId", "workspaceId"}
    assert template["bindingRef"] == operator_context().binding_ref
    assert EXCHANGE_PROFILE_ALIAS_RE.fullmatch(template["bindingRef"])
    serialized = json.dumps(template).casefold()
    assert "token" not in serialized
    assert "secret" not in serialized
    assert "mail" not in serialized
    assert "officev2" not in serialized


def test_office_setup_runtime_and_contract_are_in_release_tool_closure() -> None:
    """Package the temporary UI and its exact operator contract with deployment tools."""

    assert {
        "office_oauth_setup.py",
        "config/office-operator.schema.json",
        "config/office-operator.template.json",
    } <= release_bundle.AUTOMATION_DEPLOYMENT_TOOL_FILES


def test_operator_config_pins_exact_owner_and_workspace() -> None:
    """Parse the server-private pins and reject identity or unknown-field drift."""

    template = json.loads((DEPLOYMENT_ROOT / "config/office-operator.template.json").read_text())
    context = setup._operator_context_from_document(template)
    assert context.tenant_id == "00000000-0000-4000-8000-000000000001"
    assert context.external_subject == f"{context.tenant_id}:00000000-0000-4000-8000-000000000002"
    assert context.workspace_id == "00000000-0000-4000-8000-000000000004"
    invalid = dict(template)
    invalid["oauthToken"] = "must-not-exist"
    with pytest.raises(setup.OfficeSetupError, match="office_operator_config_invalid"):
        setup._operator_context_from_document(invalid)


def test_private_operator_config_requires_owner_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read only a regular, single-link, owner-only JSON file without following symlinks."""

    template = json.loads((DEPLOYMENT_ROOT / "config/office-operator.template.json").read_text())
    path = tmp_path / "office-operator.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(setup, "OWNER_UID", os.geteuid())
    context = setup.load_private_operator_config(str(path))
    assert context.workspace_id == "00000000-0000-4000-8000-000000000004"
    path.chmod(0o644)
    with pytest.raises(setup.OfficeSetupError, match="office_operator_config_permissions_invalid"):
        setup.load_private_operator_config(str(path))


def test_cli_bridge_uses_fixed_argv_and_keeps_callback_values_on_stdin() -> None:
    """Never put OAuth code/state in process arguments, stderr, or command text."""

    seen: dict[str, Any] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b'{"ok":true}\n', stderr=b"")

    bridge = setup.ExchangeCliBridge(runner=runner)
    response = bridge.invoke("complete", {"state": "state-secret", "code": "code-secret"})
    assert response == {"ok": True}
    command = seen["command"]
    assert command == [
        setup.EXCHANGE_CLI,
        "integrations",
        "office-setup",
        "complete",
        "--integration-database",
        setup.INTEGRATION_DATABASE,
        "--private-config",
        setup.PRIVATE_OPERATOR_CONFIG,
    ]
    assert "state-secret" not in command and "code-secret" not in command
    assert json.loads(seen["input"]) == {"state": "state-secret", "code": "code-secret"}
    assert seen["stderr"] == subprocess.DEVNULL


def test_cli_bridge_rejects_multiline_and_non_json_output() -> None:
    """Only one safe JSON result line may cross from the fixed CLI to the page."""

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, stdout=b'{"ok":true}\nsecret\n', stderr=b"")

    with pytest.raises(setup.OfficeSetupError, match="office_bridge_response_invalid"):
        setup.ExchangeCliBridge(runner=runner).invoke("status", {})


@pytest.mark.parametrize(
    "operation",
    ["begin-mail-scope-upgrade", "complete-scope-upgrade"],
)
def test_cli_bridge_exposes_only_fixed_scope_upgrade_operations(operation: str) -> None:
    """Add fixed OAuth upgrade verbs without allowing arbitrary operation selectors."""

    seen: dict[str, Any] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen["command"] = command
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b'{"ok":true}\n', stderr=b"")

    payload = (
        {"requestId": "fixed-request-id"}
        if operation.startswith("begin-")
        else {
            "state": "state-secret",
            "code": "code-secret",
        }
    )
    response = setup.ExchangeCliBridge(runner=runner).invoke(operation, payload)
    assert response == {"ok": True}
    assert seen["command"][3] == operation
    assert json.loads(seen["input"]) == payload
    assert "state-secret" not in seen["command"] and "code-secret" not in seen["command"]

    with pytest.raises(setup.OfficeSetupError, match="office_operation_invalid"):
        setup.ExchangeCliBridge(runner=runner).invoke("complete-mail-from-client", {})


def test_authorization_url_accepts_only_loopback_and_no_mail_scope() -> None:
    """Pin Microsoft sign-in to the registered localhost callback and deny Mail scopes."""

    url = authorization_url(setup.DEFAULT_PORT)
    assert (
        setup._validate_authorization_url(
            url,
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )
        == url
    )
    with pytest.raises(setup.OfficeSetupError, match="office_callback_uri_invalid"):
        setup._validate_authorization_url(
            url.replace("localhost", "127.0.0.1"),
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )
    mail_url = authorization_url(
        setup.DEFAULT_PORT,
        scopes="openid https://graph.microsoft.com/Mail.Read",
    )
    with pytest.raises(setup.OfficeSetupError, match="office_mail_scope_forbidden"):
        setup._validate_authorization_url(
            mail_url,
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )
    with pytest.raises(setup.OfficeSetupError, match="office_authorization_url_invalid"):
        setup._validate_authorization_url(
            authorization_url(setup.DEFAULT_PORT, authority=SUBJECT),
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )
    no_refresh = authorization_url(
        setup.DEFAULT_PORT,
        scopes="openid profile User.Read Tasks.ReadWrite Calendars.ReadWrite Sites.Selected",
    )
    with pytest.raises(setup.OfficeSetupError, match="office_oauth_scope_policy_invalid"):
        setup._validate_authorization_url(
            no_refresh,
            port=setup.DEFAULT_PORT,
            tenant_id=TENANT,
            enabled_features=ENABLED_FEATURES,
        )


def test_mail_oauth_scopes_are_minimal_and_exact_for_opted_in_features() -> None:
    """Derive Basic/ReadWrite/Send grants only from explicit Mail flags."""

    cases = (
        (mail_features(metadata=True), {"Mail.ReadBasic"}),
        (mail_features(drafts=True), {"Mail.ReadWrite"}),
        (mail_features(send=True), {"Mail.ReadBasic", "Mail.Send"}),
        (mail_features(drafts=True, send=True), {"Mail.ReadWrite", "Mail.Send"}),
    )
    for features, expected_mail_scopes in cases:
        scopes = scopes_for_features(features)
        actual_mail_scopes = {scope for scope in scopes if scope.startswith("Mail.")}
        assert actual_mail_scopes == expected_mail_scopes
        setup._validate_scope_policy(scopes, features)

    with pytest.raises(setup.OfficeSetupError, match="office_mail_scope_forbidden"):
        invalid_mail_features = mail_features(metadata=True)
        setup._safe_status(
            status_document(
                state="LINKED",
                auth_status="healthy",
                external_subject=EXTERNAL_SUBJECT,
                revision=0,
                target_scope={"workspaceId": WORKSPACE},
                scopes=SCOPES + ["Mail.Read"],
                enabled_features=invalid_mail_features,
                mail_authorization_state="READY",
            ),
            operator_context(),
        )


def test_begin_and_callback_keep_code_and_state_inside_cli_stdin() -> None:
    """Exchange validates the OAuth callback; the setup service does not retain its values."""

    bridge = FakeBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    auth_url = service.begin(session)
    state_present = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query).get("state") == [
        "state-secret-123"
    ]
    assert state_present
    service.complete_callback(
        "state=state-secret-123&code=code-secret-456&session_state=discard-me&client_info=discard-too"
    )
    operation, payload = bridge.calls[-1]
    assert operation == "complete"
    assert payload == {"state": "state-secret-123", "code": "code-secret-456"}
    code_redacted = "code-secret-456" not in repr(service._active_flow)
    assert code_redacted
    safe = service.status(session)
    assert safe["identityVerified"] is True
    assert safe["credentialRevision"] == 0
    assert safe["message"] == "AUTHORIZATION_COMPLETE"
    assert "externalSubject" not in safe
    service.discover(session, "todo_lists")
    targets = {
        "plannerPlanIds": [],
        "sharePointListIds": [],
        "sharePointDriveIds": [],
        "toDoTaskListIds": ["todo-1"],
        "calendarIds": [],
        "freebusySubjects": [],
    }
    updated = service.select_targets(session, expected_revision=0, targets_value=targets)
    assert updated["credentialRevision"] == 1
    final_status = service.status(session)
    assert final_status["identityVerified"] is True
    assert final_status["credentialRevision"] == 1


def test_explicit_mail_scope_upgrade_uses_server_flow_kind_and_preserves_owner_profile() -> None:
    """Upgrade a healthy linked profile only from the explicit server-selected Mail flow."""

    features = mail_features(metadata=True, drafts=True, send=True)
    bridge = FakeBridge()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=4,
        target_scope={"workspaceId": WORKSPACE},
        scopes=SCOPES,
        enabled_features=features,
        mail_authorization_state="REAUTHORIZATION_REQUIRED",
    )
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()

    authorization = service.begin(session, mail_scope_upgrade=True)
    operation, request = bridge.calls[-1]
    assert operation == "begin-mail-scope-upgrade"
    assert set(request) == {"requestId"}
    assert re.fullmatch(r"[0-9a-f]{32}", request["requestId"])
    assert service._active_flow is not None
    assert service._active_flow.flow_kind == "scope_upgrade"
    parsed_scopes = urllib.parse.parse_qs(urllib.parse.urlsplit(authorization).query)["scope"][
        0
    ].split()
    assert {scope for scope in parsed_scopes if scope.startswith("Mail.")} == {
        "Mail.ReadWrite",
        "Mail.Send",
    }

    with pytest.raises(setup.OfficeSetupError, match="office_callback_flow_mismatch"):
        service.complete_callback("state=other-state&code=must-not-be-forwarded")
    assert not any(operation == "complete-scope-upgrade" for operation, _ in bridge.calls)

    query = urllib.parse.urlencode(
        {
            "state": "state-secret-123",
            "code": "code-secret-456",
            "scope": " ".join(scopes_for_features(features)),
        }
    )
    service.complete_callback(query)
    operation, payload = bridge.calls[-1]
    assert operation == "complete-scope-upgrade"
    assert payload == {"state": "state-secret-123", "code": "code-secret-456"}
    assert bridge.status_value["profileRef"] == PROFILE_REF
    assert bridge.status_value["mailAuthorizationState"] == "READY"
    assert service.status(session)["mailAuthorizationState"] == "READY"


def test_mail_scope_upgrade_requires_explicit_need_and_exchange_flow_binding() -> None:
    """Reject no-op upgrades and a link response returned for the upgrade operation."""

    features = mail_features(metadata=True)
    bridge = FakeBridge()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=4,
        target_scope={"workspaceId": WORKSPACE},
        scopes=SCOPES + ["Mail.ReadBasic"],
        enabled_features=features,
        mail_authorization_state="READY",
    )
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    with pytest.raises(setup.OfficeSetupError, match="office_mail_upgrade_not_required"):
        service.begin(session, mail_scope_upgrade=True)
    assert not any(operation == "begin-mail-scope-upgrade" for operation, _ in bridge.calls)

    bridge.status_value["mailAuthorizationState"] = "REAUTHORIZATION_REQUIRED"
    bridge.status_value["requiredOAuthScopes"] = list(SCOPES)
    bridge.flow_kind_override = "link"
    with pytest.raises(setup.OfficeSetupError, match="office_begin_flow_kind_invalid"):
        service.begin(session, mail_scope_upgrade=True)
    assert service._active_flow is None


def test_legacy_begin_may_omit_flow_kind_but_mail_upgrade_may_not() -> None:
    """Keep old initial-link response compatibility without guessing upgrade callbacks."""

    bridge = FakeBridge()
    bridge.omit_flow_kind = True
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    service.begin(session)
    assert service._active_flow is not None
    assert service._active_flow.flow_kind == "link"

    features = mail_features(metadata=True)
    upgrade_bridge = FakeBridge()
    upgrade_bridge.omit_flow_kind = True
    upgrade_bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=1,
        target_scope={"workspaceId": WORKSPACE},
        scopes=SCOPES,
        enabled_features=features,
        mail_authorization_state="REAUTHORIZATION_REQUIRED",
    )
    upgrade_service = setup.OfficeSetupService(upgrade_bridge, operator_context=operator_context())
    _, upgrade_session = upgrade_service.create_session()
    with pytest.raises(setup.OfficeSetupError, match="office_begin_flow_kind_invalid"):
        upgrade_service.begin(upgrade_session, mail_scope_upgrade=True)


def test_begin_reopens_same_unexpired_provider_flow_and_replaces_only_after_expiry() -> None:
    """Reuse Exchange's verified URL for one session and use its expiry, not local TTL."""

    bridge = FakeBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    first_url = service.begin(session)
    flow = service._active_flow

    assert flow is not None
    same_url = hmac.compare_digest(service.begin(session), first_url)
    assert same_url
    assert bridge.begin_calls == 1
    stored_url_matches = hmac.compare_digest(flow.authorization_url, first_url)
    state_redacted = "state-secret-123" not in repr(flow)
    csrf_redacted = session.csrf_token not in repr(flow)
    assert stored_url_matches and state_redacted and csrf_redacted

    flow.expires_at = setup.datetime(2000, 1, 1, tzinfo=setup.UTC)
    replacement_url = service.begin(session)
    replacement_is_new = not hmac.compare_digest(replacement_url, first_url)
    assert replacement_is_new
    assert bridge.begin_calls == 2


def test_begin_does_not_share_an_unexpired_flow_with_another_browser_session() -> None:
    """A second cookie session cannot receive or replace the first session's URL."""

    bridge = FakeBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, owner_session = service.create_session()
    _, other_session = service.create_session()
    first_url = service.begin(owner_session)

    with pytest.raises(setup.OfficeSetupError, match="office_authorization_pending"):
        service.begin(other_session)

    assert bridge.begin_calls == 1
    assert service._active_flow is not None
    assert service._active_flow.session is owner_session
    stored_url_matches = hmac.compare_digest(service._active_flow.authorization_url, first_url)
    assert stored_url_matches


def test_concurrent_begin_calls_create_one_provider_flow() -> None:
    """Reject a concurrent duplicate promptly instead of queuing another begin."""

    bridge = BlockingBeginBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.begin, session)
        assert bridge.begin_started.wait(timeout=2)
        with pytest.raises(setup.OfficeSetupError, match="office_authorization_pending"):
            service.begin(session)
        bridge.release_begin.set()
        first_url = first.result(timeout=2)

    assert bridge.begin_calls == 1
    assert urllib.parse.urlsplit(first_url).hostname == "login.microsoftonline.com"


def test_status_poll_is_singleflight_and_reuses_only_fresh_validated_cache() -> None:
    """Do not queue status CLI calls; a fresh safe snapshot avoids repeated cold RPCs."""

    bridge = BlockingStatusBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.status, session)
        assert bridge.status_started.wait(timeout=2)
        with pytest.raises(setup.OfficeSetupError, match="office_status_busy"):
            service.status(session)
        bridge.release_status.set()
        assert first.result(timeout=2)["authStatus"] == "unknown"

    assert len([op for op, _ in bridge.calls if op == "status"]) == 1
    assert service.status(session)["authStatus"] == "unknown"
    assert len([op for op, _ in bridge.calls if op == "status"]) == 1
    service._status_cache_at = time.monotonic() - setup.STATUS_CACHE_TTL_SECONDS - 1
    assert service.status(session)["authStatus"] == "unknown"
    assert len([op for op, _ in bridge.calls if op == "status"]) == 2


def test_begin_reuses_fresh_status_snapshot_and_invalidates_after_begin() -> None:
    """A status checked seconds ago is reused for begin, then invalidated after mutation."""

    bridge = FakeBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    service.status(session)

    service.begin(session)
    assert [op for op, _ in bridge.calls].count("status") == 1
    assert [op for op, _ in bridge.calls].count("begin") == 1

    service.status(session)
    assert [op for op, _ in bridge.calls].count("status") == 2


def test_select_targets_invalidates_the_cached_status_snapshot(
    linked_service: tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession],
) -> None:
    """A target revision change forces the next poll to read the updated owner state."""

    service, bridge, session = linked_service
    service.status(session)
    service.discover(session, "todo_lists")
    targets = {
        "plannerPlanIds": [],
        "sharePointListIds": [],
        "sharePointDriveIds": [],
        "toDoTaskListIds": ["todo-1"],
        "calendarIds": [],
        "freebusySubjects": [],
    }
    service.select_targets(session, expected_revision=0, targets_value=targets)

    updated = service.status(session)
    assert updated["credentialRevision"] == 1
    assert [op for op, _ in bridge.calls].count("status") == 2


def test_begin_reuses_status_that_finishes_while_it_waits_for_singleflight_lock() -> None:
    """A poll already in flight can satisfy begin without a duplicate cold status call."""

    bridge = BlockingStatusBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()

    with ThreadPoolExecutor(max_workers=2) as pool:
        status = pool.submit(service.status, session)
        assert bridge.status_started.wait(timeout=2)
        begin = pool.submit(service.begin, session)
        # Let begin establish its pending state before the status bridge completes.
        deadline = time.monotonic() + 2
        while not service._begin_pending and time.monotonic() < deadline:
            time.sleep(0.005)
        assert service._begin_pending
        bridge.release_status.set()
        assert status.result(timeout=2)["authStatus"] == "unknown"
        location = begin.result(timeout=2)

    assert urllib.parse.urlsplit(location).hostname == "login.microsoftonline.com"
    assert len([op for op, _ in bridge.calls if op == "status"]) == 1
    assert bridge.begin_calls == 1


def test_new_begin_marks_cached_poll_as_preparing_not_previous_failure() -> None:
    """A prior failure cannot clear a new attempt's popup while status is in flight."""

    bridge = BlockingStatusBeginBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    with service._lock:
        service._last_callback_message = "AUTHORIZATION_DID_NOT_COMPLETE"

    with ThreadPoolExecutor(max_workers=2) as pool:
        status = pool.submit(service.status, session)
        assert bridge.status_started.wait(timeout=2)
        begin = pool.submit(service.begin, session)
        deadline = time.monotonic() + 2
        while not service._begin_pending and time.monotonic() < deadline:
            time.sleep(0.005)
        assert service._begin_pending
        with service._lock:
            assert service._last_callback_message == "AUTHORIZATION_PREPARING"

        bridge.release_status.set()
        assert status.result(timeout=2)["message"] == "AUTHORIZATION_PREPARING"
        assert bridge.begin_started.wait(timeout=2)
        with pytest.raises(setup.OfficeSetupError, match="office_status_busy"):
            service.status(session)
        bridge.release_begin.set()
        location = begin.result(timeout=2)

    assert urllib.parse.urlsplit(location).hostname == "login.microsoftonline.com"
    assert bridge.begin_calls == 1
    assert [op for op, _ in bridge.calls].count("status") == 1


def test_rejected_begin_clears_preparing_state_and_allows_a_later_attempt() -> None:
    """A rejected or malformed begin result must not strand the UI in preparing state."""

    class RejectOnceBridge(FakeBridge):
        reject_next_begin = True

        def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            if operation == "begin" and self.reject_next_begin:
                self.reject_next_begin = False
                raise setup.OfficeSetupError("office_bridge_rejected")
            return super().invoke(operation, payload)

    bridge = RejectOnceBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()

    with pytest.raises(setup.OfficeSetupError, match="office_bridge_rejected"):
        service.begin(session)
    assert service.status(session)["message"] == "AUTHORIZATION_DID_NOT_COMPLETE"
    assert service.begin(session).startswith("https://login.microsoftonline.com/")
    assert bridge.begin_calls == 1


def test_blocked_begin_keeps_home_and_session_responsive_and_status_unqueued() -> None:
    """A slow owner RPC must not hold the service lock or accumulate status calls."""

    bridge = BlockingStatusBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, _page = _page_session(port)
        form = urllib.parse.urlencode({"csrf": csrf})

        def submit_authorize() -> tuple[int, str]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(
                "POST",
                "/authorize",
                body=form,
                headers={
                    "Host": f"localhost:{port}",
                    "Origin": f"http://localhost:{port}",
                    "Cookie": cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(form)),
                },
            )
            response = connection.getresponse()
            status = response.status
            location = response.getheader("Location", "")
            response.read()
            connection.close()
            return status, location

        with ThreadPoolExecutor(max_workers=1) as pool:
            authorization = pool.submit(submit_authorize)
            assert bridge.status_started.wait(timeout=2)

            # The homepage creates another session while Exchange is blocked.
            new_cookie, _new_csrf, _new_page = _page_session(port)
            assert new_cookie

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request(
                "GET", "/api/status", headers={"Host": f"localhost:{port}", "Cookie": cookie}
            )
            status_response = connection.getresponse()
            status_body = json.loads(status_response.read().decode("utf-8"))
            assert status_response.status == HTTPStatus.SERVICE_UNAVAILABLE
            assert status_body == {"error": "office_status_busy"}
            connection.close()

            assert len([op for op, _ in bridge.calls if op == "status"]) == 0
            bridge.release_status.set()
            result_status, location = authorization.result(timeout=3)
            assert result_status == HTTPStatus.SEE_OTHER
            assert urllib.parse.urlsplit(location).hostname == "login.microsoftonline.com"

        assert len([op for op, _ in bridge.calls if op == "status"]) == 1
        assert bridge.begin_calls == 1
    finally:
        bridge.release_status.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "expires_at",
    [
        "not-a-timestamp",
        "2099-01-01T00:00:00",
        "2000-01-01T00:00:00Z",
        "0001-01-01T00:00:00+23:59",
    ],
)
def test_begin_rejects_invalid_or_expired_provider_url_expiry(expires_at: str) -> None:
    """Never retain or redirect to a URL with malformed, naive, or expired provider expiry."""

    bridge = FakeBridge()
    bridge.expires_at = expires_at
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()

    with pytest.raises(setup.OfficeSetupError, match="office_begin_response_invalid"):
        service.begin(session)

    assert service._active_flow is None


def test_status_requires_actor_owned_matching_provider_and_never_mail_scope() -> None:
    """Reject unexpected owner access modes and any accidental Mail grant."""

    bad = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
        scopes=["openid", "Mail.Read"],
    )
    with pytest.raises(setup.OfficeSetupError, match="office_mail_scope_forbidden"):
        setup._safe_status(bad, operator_context())
    bad_owner = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
    )
    bad_owner["accessMode"] = "workspace_operator"
    assert setup._safe_status(bad_owner, operator_context())["identityVerified"] is False


def test_discovery_and_cas_selection_accept_only_server_observed_targets(
    linked_service: tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession],
) -> None:
    """Pass only discovered IDs through the fixed owner-bound CAS target update."""

    service, bridge, session = linked_service
    for resource_type in setup.ALLOWED_DISCOVERY_TYPES - {"mail_folders"}:
        service.discover(session, resource_type)
    targets = {
        "plannerPlanIds": ["plan-1"],
        "sharePointListIds": [],
        "sharePointDriveIds": ["drive-1"],
        "toDoTaskListIds": ["todo-1"],
        "calendarIds": ["calendar-1"],
        "freebusySubjects": ["person@dohorizon.com"],
        "mailFolderIds": [],
    }
    result = service.select_targets(session, expected_revision=0, targets_value=targets)
    assert result["state"] == "UPDATED"
    assert result["credentialRevision"] == 1
    operation, payload = bridge.calls[-1]
    assert operation == "select-targets"
    assert payload == {"expectedCredentialRevision": 0, "targets": targets}
    assert "scopes" not in payload and "tenantId" not in payload and "workspaceId" not in payload
    assert service.status(session)["credentialRevision"] == 1


def test_mail_folder_discovery_and_selection_are_opt_in_and_server_observed() -> None:
    """Only Mail metadata opt-in can discover and select a server-returned folder ID."""

    features = mail_features(metadata=True)
    bridge = FakeBridge()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=2,
        target_scope={"workspaceId": WORKSPACE},
        scopes=SCOPES + ["Mail.ReadBasic"],
        enabled_features=features,
        mail_authorization_state="READY",
    )
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()

    result = service.discover(session, "mail_folders")
    assert result["items"] == [
        {"id": "mail-folder-1", "displayName": "Inbox", "kind": "mail_folder"}
    ]
    targets = {
        "plannerPlanIds": [],
        "sharePointListIds": [],
        "sharePointDriveIds": [],
        "toDoTaskListIds": [],
        "calendarIds": [],
        "freebusySubjects": [],
        "mailFolderIds": ["mail-folder-1"],
    }
    updated = service.select_targets(session, expected_revision=2, targets_value=targets)
    assert updated["selectedCounts"]["mailFolders"] == 1
    operation, payload = bridge.calls[-1]
    assert operation == "select-targets"
    assert payload["targets"] == targets


def test_mail_folders_require_ready_scope_and_exact_discovery_kind() -> None:
    """Do not issue folder reads before consent or accept arbitrary resources as folders."""

    features = mail_features(metadata=True)
    bridge = FakeBridge()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=2,
        target_scope={"workspaceId": WORKSPACE},
        scopes=SCOPES,
        enabled_features=features,
        mail_authorization_state="REAUTHORIZATION_REQUIRED",
    )
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    with pytest.raises(setup.OfficeSetupError, match="office_mail_authorization_required"):
        service.discover(session, "mail_folders")
    assert not any(operation == "discover" for operation, _ in bridge.calls)

    bridge.status_value["requiredOAuthScopes"] = SCOPES + ["Mail.ReadBasic"]
    bridge.status_value["mailAuthorizationState"] = "READY"
    bridge.discoveries["mail_folders"]["items"][0]["kind"] = "message"
    with service._lock:
        service._invalidate_status_locked()
    with pytest.raises(setup.OfficeSetupError, match="office_discovery_item_invalid"):
        service.discover(session, "mail_folders")


def test_mail_upgrade_status_keeps_existing_non_mail_profile_usable() -> None:
    """A missing optional Mail grant does not invalidate an otherwise healthy owner link."""

    features = mail_features(metadata=True, drafts=True)
    status = setup._safe_status(
        status_document(
            state="LINKED",
            auth_status="healthy",
            external_subject=EXTERNAL_SUBJECT,
            revision=3,
            target_scope={"workspaceId": WORKSPACE},
            scopes=SCOPES,
            enabled_features=features,
            mail_authorization_state="REAUTHORIZATION_REQUIRED",
        ),
        operator_context(),
    )
    assert status["identityVerified"] is True
    assert status["reauthRequired"] is False
    assert status["mailAuthorizationState"] == "REAUTHORIZATION_REQUIRED"

    legacy_status = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=3,
        target_scope={"workspaceId": WORKSPACE},
        scopes=SCOPES,
        enabled_features=features,
    )
    assert "mailAuthorizationState" not in legacy_status
    derived = setup._safe_status(legacy_status, operator_context())
    assert derived["identityVerified"] is True
    assert derived["mailAuthorizationState"] == "REAUTHORIZATION_REQUIRED"

    malformed_status = dict(legacy_status, mailAuthorizationState=None)
    with pytest.raises(setup.OfficeSetupError, match="office_status_mail_authorization_invalid"):
        setup._safe_status(malformed_status, operator_context())


def test_discovery_follows_opaque_continuation_only_inside_the_server() -> None:
    """Collect complete owner metadata pages and never expose a signed provider cursor."""

    class PagingBridge(FakeBridge):
        def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            if operation != "discover" or payload.get("resourceType") != "todo_lists":
                return super().invoke(operation, payload)
            self.calls.append((operation, dict(payload)))
            cursor = payload.get("pageCursor")
            if cursor is None:
                return {
                    "state": "DISCOVERED",
                    "resourceType": "todo_lists",
                    "items": [{"id": "todo-1", "displayName": "One", "kind": "task_list"}],
                    "pageCursor": None,
                    "nextCursor": "signed-cursor-private",
                    "complete": False,
                    "truncated": False,
                    "credentialRevision": 0,
                }
            return {
                "state": "DISCOVERED",
                "resourceType": "todo_lists",
                "items": [{"id": "todo-2", "displayName": "Two", "kind": "task_list"}],
                "pageCursor": cursor,
                "nextCursor": None,
                "complete": True,
                "truncated": False,
                "credentialRevision": 0,
            }

    bridge = PagingBridge()
    service = setup.OfficeSetupService(bridge, operator_context=operator_context())
    _, session = service.create_session()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
    )
    discovered = service.discover(session, "todo_lists")
    assert [item["id"] for item in discovered["items"]] == ["todo-1", "todo-2"]
    assert "signed-cursor-private" not in json.dumps(discovered)
    page_calls = [payload for operation, payload in bridge.calls if operation == "discover"]
    assert page_calls == [
        {"resourceType": "todo_lists"},
        {"resourceType": "todo_lists", "pageCursor": "signed-cursor-private"},
    ]


def test_cas_rejects_ids_not_returned_by_exchange_discovery(
    linked_service: tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession],
) -> None:
    """The page cannot widen the target set by posting an arbitrary Graph resource ID."""

    service, bridge, session = linked_service
    service.discover(session, "todo_lists")
    targets = {
        "plannerPlanIds": [],
        "sharePointListIds": [],
        "sharePointDriveIds": [],
        "toDoTaskListIds": ["attacker-chosen-id"],
        "calendarIds": [],
        "freebusySubjects": [],
    }
    before = len(bridge.calls)
    with pytest.raises(setup.OfficeSetupError, match="office_target_not_discovered"):
        service.select_targets(session, expected_revision=0, targets_value=targets)
    assert len(bridge.calls) == before
    assert not any(operation == "select-targets" for operation, _ in bridge.calls[before:])


def test_freebusy_selection_cannot_clear_a_nonempty_host_allowlist(
    linked_service: tuple[setup.OfficeSetupService, FakeBridge, setup.SetupSession],
) -> None:
    """Keep at least one fixed host-authorized person when Calendar free/busy is enabled."""

    service, bridge, session = linked_service
    service.discover(session, "freebusy_subjects")
    targets = {
        "plannerPlanIds": [],
        "sharePointListIds": [],
        "sharePointDriveIds": [],
        "toDoTaskListIds": [],
        "calendarIds": [],
        "freebusySubjects": [],
    }
    before = len(bridge.calls)
    with pytest.raises(setup.OfficeSetupError, match="office_freebusy_target_required"):
        service.select_targets(session, expected_revision=0, targets_value=targets)
    assert not any(operation == "select-targets" for operation, _ in bridge.calls[before:])


def _start_server(bridge: FakeBridge) -> tuple[setup.OfficeSetupServer, threading.Thread, int]:
    """Start one local HTTP server and bind the fake OAuth URL to its assigned port."""

    service = setup.OfficeSetupService(bridge, operator_context=operator_context(), port=0)
    server = setup.OfficeSetupServer(("127.0.0.1", 0), service, server_nonce="test-nonce")
    port = server.server_address[1]
    service.port = port
    bridge.port = port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _page_session(port: int) -> tuple[str, str, str]:
    """Create a browser session through the canonical localhost origin."""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
    response = connection.getresponse()
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.getheader("Location") == f"http://localhost:{port}/"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    response.read()
    connection.request("GET", "/", headers={"Host": f"localhost:{port}"})
    response = connection.getresponse()
    page = response.read().decode("utf-8")
    assert response.status == HTTPStatus.OK
    assert response.getheader("Referrer-Policy") == "same-origin"
    csp = response.getheader("Content-Security-Policy", "")
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    form_action = next(
        part.strip() for part in csp.split(";") if part.strip().startswith("form-action ")
    )
    assert form_action == "form-action 'self' https://login.microsoftonline.com"
    cookie = response.getheader("Set-Cookie").split(";", maxsplit=1)[0]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page)
    assert csrf is not None
    connection.close()
    return cookie, csrf.group(1), page


def test_loopback_accepts_localhost_and_127_but_requires_matching_origin_and_csrf() -> None:
    """Accept matching loopback spellings while rejecting cross-origin and invalid-CSRF writes."""

    bridge = FakeBridge()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=0,
        target_scope={"workspaceId": WORKSPACE},
    )
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, page = _page_session(port)
        assert "Account Center" in page
        assert "Tasks.Read" not in page
        assert "Mail.Read" not in page

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "POST",
            "/api/discover",
            body=json.dumps({"csrf": csrf, "resourceType": "todo_lists"}),
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-Cyrene-CSRF": csrf,
            },
        )
        mismatched = connection.getresponse()
        assert mismatched.status == HTTPStatus.FORBIDDEN
        mismatched.read()
        assert bridge.calls == []

        for external_origin in ("https://attacker.example", "null"):
            connection.request(
                "POST",
                "/api/discover",
                body=json.dumps({"csrf": csrf, "resourceType": "todo_lists"}),
                headers={
                    "Host": f"localhost:{port}",
                    "Origin": external_origin,
                    "Cookie": cookie,
                    "Content-Type": "application/json",
                    "X-Cyrene-CSRF": csrf,
                },
            )
            external = connection.getresponse()
            assert external.status == HTTPStatus.FORBIDDEN
            external.read()
            assert bridge.calls == []

        connection.request(
            "POST",
            "/api/discover",
            body=json.dumps({"csrf": csrf, "resourceType": "todo_lists"}),
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-Cyrene-CSRF": "wrong",
            },
        )
        csrf_denied = connection.getresponse()
        assert csrf_denied.status == HTTPStatus.FORBIDDEN
        csrf_denied.read()
        assert bridge.calls == []

        connection.request(
            "POST",
            "/api/discover",
            body=json.dumps({"csrf": csrf, "resourceType": "todo_lists"}),
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "Cookie": cookie,
                "Content-Type": "application/json",
                "X-Cyrene-CSRF": csrf,
            },
        )
        accepted = connection.getresponse()
        result = json.loads(accepted.read().decode("utf-8"))
        assert accepted.status == HTTPStatus.OK
        assert result["items"][0]["id"] == "todo-1"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_reused_homepage_keeps_same_origin_referrer_policy_and_api_stays_no_referrer() -> None:
    """Limit same-origin referrers to page GETs while API responses stay no-referrer."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, _csrf, page_body = _page_session(port)
        assert "Requesting Microsoft sign-in…" in page_body
        assert "AUTHORIZATION_WAIT_NOTICE_MS=15000" in page_body
        assert "AUTHORIZATION_WAIT_TIMEOUT_MS=135000" in page_body
        assert "STATUS_REQUEST_TIMEOUT_MS=8000" in page_body
        assert "new AbortController()" in page_body
        assert "refreshInFlight" in page_body
        assert "AUTHORIZATION_PREPARING" in page_body
        assert "setInterval(" not in page_body
        assert 'target="cyrene-office-oauth"' in page_body
        assert "try{authPopupDocument=popup.document;}catch(_error){}" in page_body
        auth_submit = page_body.split("for(const form of [document.getElementById('authorize')", 1)[
            1
        ].split("\ndocument.getElementById('discover')", 1)[0]
        assert 'method="post" action="/authorize"' in page_body
        assert 'action="/authorize-mail-scope-upgrade"' in page_body
        assert "mailAuthorizationState==='REAUTHORIZATION_REQUIRED'" in page_body
        popup_access_fallback = auth_submit.split("authPopupDocument=null;", 1)[1]
        assert "event.preventDefault()" not in popup_access_fallback
        assert "setPopupMessage('Requesting Microsoft sign-in…')" in popup_access_fallback
        assert "state-secret" not in page_body
        assert "login.microsoftonline.com/organizations/oauth2" not in page_body
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/", headers={"Host": f"localhost:{port}", "Cookie": cookie})
        page = connection.getresponse()
        assert page.status == HTTPStatus.OK
        assert page.getheader("Referrer-Policy") == "same-origin"
        assert "https://login.microsoftonline.com" in page.getheader("Content-Security-Policy", "")
        page.read()

        connection.request(
            "GET", "/api/status", headers={"Host": f"localhost:{port}", "Cookie": cookie}
        )
        status = connection.getresponse()
        assert status.status == HTTPStatus.OK
        assert status.getheader("Referrer-Policy") == "no-referrer"
        status.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_authorize_bridge_busy_renders_safe_recoverable_popup_page() -> None:
    """A known-not-executed busy result gives the popup useful text without reflecting data."""

    class BusyBridge(FakeBridge):
        def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            if operation == "status":
                raise setup.OfficeSetupError("office_bridge_busy")
            return super().invoke(operation, payload)

    bridge = BusyBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, _page = _page_session(port)
        form = urllib.parse.urlencode({"csrf": csrf})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "POST",
            "/authorize",
            body=form,
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(form)),
            },
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
        assert response.getheader("Content-Type", "").startswith("text/html")
        assert "Office is busy preparing sign-in" in body
        assert "Return to the Office setup page" in body
        assert "state-secret" not in body
        assert "authorizationUrl" not in body
        assert "login.microsoftonline.com" not in body
        assert bridge.begin_calls == 0
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_authorize_form_rejects_null_and_external_origin_before_cli() -> None:
    """Keep the native authorize form fail-closed for null and foreign origins."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, _page = _page_session(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        form = urllib.parse.urlencode({"csrf": csrf})
        for origin in ("null", "https://attacker.example"):
            connection.request(
                "POST",
                "/authorize",
                body=form,
                headers={
                    "Host": f"localhost:{port}",
                    "Origin": origin,
                    "Cookie": cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(form)),
                },
            )
            rejected = connection.getresponse()
            assert rejected.status == HTTPStatus.FORBIDDEN
            assert json.loads(rejected.read().decode("utf-8")) == {"error": "origin_not_allowed"}
            assert bridge.calls == []
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_authorize_form_repeated_post_reuses_same_url_only_for_its_cookie_session() -> None:
    """Exercise the real handler state: same-session retries redirect identically once."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, _page = _page_session(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        form = urllib.parse.urlencode({"csrf": csrf})
        locations: list[str | None] = []
        bodies: list[str] = []
        for _ in range(2):
            connection.request(
                "POST",
                "/authorize",
                body=form,
                headers={
                    "Host": f"localhost:{port}",
                    "Origin": f"http://localhost:{port}",
                    "Cookie": cookie,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(form)),
                },
            )
            response = connection.getresponse()
            locations.append(response.getheader("Location"))
            bodies.append(response.read().decode("utf-8"))
            assert response.status == HTTPStatus.SEE_OTHER

        first_url = urllib.parse.urlsplit(locations[0] or "")
        same_location = hmac.compare_digest(locations[0] or "", locations[1] or "")
        assert same_location
        assert first_url.scheme == "https"
        assert first_url.hostname == "login.microsoftonline.com"
        assert first_url.path.endswith("/oauth2/v2.0/authorize")
        bodies_contain_state = any("state-secret" in body for body in bodies)
        assert not bodies_contain_state
        assert bridge.begin_calls == 1

        other_cookie, other_csrf, _other_page = _page_session(port)
        other_form = urllib.parse.urlencode({"csrf": other_csrf})
        connection.request(
            "POST",
            "/authorize",
            body=other_form,
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
                "Cookie": other_cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(other_form)),
            },
        )
        pending = connection.getresponse()
        pending_body = pending.read().decode("utf-8")
        assert pending.status == HTTPStatus.CONFLICT
        assert pending.getheader("Content-Type", "").startswith("text/html")
        pending_error = (
            "office_authorization_pending" if "already being prepared" in pending_body else None
        )
        has_pending_redirect = pending.getheader("Location") is not None
        pending_body_contains_state = "state-secret" in pending_body
        assert pending_error == "office_authorization_pending"
        assert not has_pending_redirect
        assert not pending_body_contains_state
        assert bridge.begin_calls == 1
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mail_scope_upgrade_has_separate_explicit_csrf_protected_authorize_route() -> None:
    """Only the dedicated user action starts Mail consent and no flow choice comes from body."""

    features = mail_features(metadata=True)
    bridge = FakeBridge()
    bridge.status_value = status_document(
        state="LINKED",
        auth_status="healthy",
        external_subject=EXTERNAL_SUBJECT,
        revision=1,
        target_scope={"workspaceId": WORKSPACE},
        scopes=SCOPES,
        enabled_features=features,
        mail_authorization_state="REAUTHORIZATION_REQUIRED",
    )
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, page = _page_session(port)
        assert 'action="/authorize-mail-scope-upgrade"' in page
        assert "REAUTHORIZATION_REQUIRED" in page
        assert "flowKind" not in page

        form = urllib.parse.urlencode({"csrf": csrf})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "POST",
            "/authorize-mail-scope-upgrade",
            body=form,
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(form)),
            },
        )
        response = connection.getresponse()
        location = response.getheader("Location", "")
        response.read()
        assert response.status == HTTPStatus.SEE_OTHER
        assert (
            "Mail.ReadBasic"
            in urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["scope"][0]
        )
        assert [operation for operation, _ in bridge.calls].count("begin-mail-scope-upgrade") == 1

        connection.request(
            "GET",
            f"{setup.CALLBACK_PATH}?state=wrong-state&code=must-not-be-forwarded",
            headers={"Host": f"localhost:{port}"},
        )
        bad_callback = connection.getresponse()
        bad_callback_body = bad_callback.read().decode("utf-8")
        assert bad_callback.status == HTTPStatus.OK
        assert "office_callback_invalid" in bad_callback_body
        assert server.service._active_flow is not None
        assert server.service._active_flow.flow_kind == "scope_upgrade"
        assert not any(operation == "complete-scope-upgrade" for operation, _ in bridge.calls)

        forged_form = urllib.parse.urlencode({"csrf": csrf, "flowKind": "scope_upgrade"})
        connection.request(
            "POST",
            "/authorize",
            body=forged_form,
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(forged_form)),
            },
        )
        forged = connection.getresponse()
        forged_payload = json.loads(forged.read().decode("utf-8"))
        assert forged.status == HTTPStatus.BAD_GATEWAY
        assert forged_payload == {"error": "request_body_invalid"}
        assert [operation for operation, _ in bridge.calls].count("begin-mail-scope-upgrade") == 1
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_callback_success_shows_query_free_completion_page_and_only_cli_receives_secrets() -> None:
    """Success gets a finished/close page; callback code and state stay out of its response."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, csrf, _ = _page_session(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        form = urllib.parse.urlencode({"csrf": csrf})
        connection.request(
            "POST",
            "/authorize",
            body=form,
            headers={
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
                "Cookie": cookie,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(form)),
            },
        )
        begun = connection.getresponse()
        location = begun.getheader("Location")
        body = begun.read().decode("utf-8")
        assert begun.status == HTTPStatus.SEE_OTHER
        assert location.startswith("https://login.microsoftonline.com/")
        assert "state-secret-123" in location
        assert "state-secret-123" not in body
        assert "code-secret" not in body

        connection.request(
            "GET",
            f"{setup.CALLBACK_PATH}?state=state-secret-123&code=code-secret-456&session_state=discard",
            headers={"Host": f"localhost:{port}"},
        )
        callback = connection.getresponse()
        callback_body = callback.read().decode("utf-8")
        assert callback.status == HTTPStatus.OK
        assert callback.getheader("Location") is None
        assert callback.getheader("Content-Type", "").startswith("text/html")
        assert callback.getheader("Referrer-Policy") == "no-referrer"
        assert callback.getheader("Cache-Control") == "no-store"
        callback_csp = callback.getheader("Content-Security-Policy", "")
        assert "default-src 'none'" in callback_csp
        assert "script-src 'nonce-test-nonce'" in callback_csp
        assert "Microsoft sign-in complete" in callback_body
        assert "You can close this window" in callback_body
        assert "history.replaceState(null,'','/oauth/callback')" in callback_body
        assert "state-secret-123" not in callback_body
        assert "code-secret-456" not in callback_body
        assert "login.microsoftonline.com" not in callback_body
        assert bridge.calls[-1] == (
            "complete",
            {"state": "state-secret-123", "code": "code-secret-456"},
        )
        status_connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        status_connection.request(
            "GET", "/api/status", headers={"Host": f"localhost:{port}", "Cookie": cookie}
        )
        status = status_connection.getresponse()
        status_data = json.loads(status.read().decode("utf-8"))
        assert status_data["message"] == "AUTHORIZATION_COMPLETE"
        status_connection.close()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_callback_provider_error_is_generic_and_never_reaches_exchange() -> None:
    """A declined consent callback does not echo provider errors or submit partial codes."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "GET",
            f"{setup.CALLBACK_PATH}?error=access_denied&error_description=private-detail&state=state-secret",
            headers={"Host": f"localhost:{port}"},
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == HTTPStatus.OK
        assert response.getheader("Location") is None
        assert "Microsoft sign-in not completed" in body
        assert "office_callback_failed" in body
        assert "start a new sign-in" in body
        assert (
            "access_denied" not in body
            and "private-detail" not in body
            and "state-secret" not in body
        )
        assert not any(operation == "complete" for operation, _ in bridge.calls)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_callback_redirect_registration_error_surfaces_only_aadsts_code() -> None:
    """Report AADSTS50011 explicitly without echoing its query or using a public fallback."""

    bridge = FakeBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, _csrf, _page = _page_session(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "GET",
            f"{setup.CALLBACK_PATH}?error=invalid_request&error_description=AADSTS50011%3A+redirect+URI+must+be+registered&state=state-secret",
            headers={"Host": f"localhost:{port}"},
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == HTTPStatus.OK
        assert response.getheader("Location") is None
        assert "AADSTS50011" in body
        assert "verify the exact redirect URI" in body
        assert "private-detail" not in body
        assert "state-secret" not in body
        assert not any(operation == "complete" for operation, _ in bridge.calls)

        connection.request(
            "GET",
            "/api/status",
            headers={"Host": f"localhost:{port}", "Cookie": cookie},
        )
        status = connection.getresponse()
        data = json.loads(status.read().decode("utf-8"))
        assert status.status == HTTPStatus.OK
        assert data["message"] == "AADSTS50011"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_callback_bridge_busy_is_shown_as_unsubmitted_not_completed() -> None:
    """A known pre-execution busy callback gets safe recovery text and is not faked as success."""

    class BusyCompleteBridge(FakeBridge):
        def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            if operation == "complete":
                raise setup.OfficeSetupError("office_bridge_busy")
            return super().invoke(operation, payload)

    bridge = BusyCompleteBridge()
    server, thread, port = _start_server(bridge)
    try:
        cookie, _csrf, _page = _page_session(port)
        session_id = cookie.split(";", 1)[0].split("=", 1)[1]
        session = server.service.get_session(session_id)
        server.service.begin(session)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "GET",
            f"{setup.CALLBACK_PATH}?state=state-secret-123&code=code-secret",
            headers={"Host": f"localhost:{port}"},
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == HTTPStatus.OK
        assert "office_bridge_busy" in body
        assert "callback was not submitted" in body
        assert "Do not reload or retry this callback" in body
        assert "Microsoft sign-in complete" not in body
        assert "state-secret" not in body
        assert "code-secret" not in body
        assert not any(operation == "complete" for operation, _ in bridge.calls)

        connection.request(
            "GET", "/api/status", headers={"Host": f"localhost:{port}", "Cookie": cookie}
        )
        status = connection.getresponse()
        data = json.loads(status.read().decode("utf-8"))
        assert data["message"] == "office_bridge_busy"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
