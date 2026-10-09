"""Contract tests for the fixed, journal-integrated Workspace Web host."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packaging" / "workload_web_host.py"
SPEC = importlib.util.spec_from_file_location("cyrene_workload_web_host_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
web_host = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = web_host
SPEC.loader.exec_module(web_host)


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _result(
    arguments: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


def _fake_runner(
    arguments: list[str], _environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    if arguments[1:2] == ["show"]:
        prop = arguments[2].split("=", 1)[1]
        values = {"LoadState": "not-found", "ActiveState": "inactive"}
        return _result(arguments, stdout=values.get(prop, "") + "\n")
    if arguments[1:2] == ["is-enabled"]:
        return _result(arguments, returncode=1, stdout="disabled\n")
    if arguments[1:2] == ["-v"]:
        return _result(arguments, stderr="nginx/1.24.0\n")
    return _result(arguments)


APT_POLICY = """nginx:
  Installed: (none)
  Candidate: 1.24.0-2ubuntu7.18
  Version table:
     1.24.0-2ubuntu7.18 500
        500 http://archive.ubuntu.com/ubuntu noble-updates/main amd64 Packages
"""
APT_SIMULATION = """Inst nginx-common (1.24.0-2ubuntu7.18 Ubuntu:24.04/noble-updates [all])
Inst nginx (1.24.0-2ubuntu7.18 Ubuntu:24.04/noble-updates [amd64])
Conf nginx-common (1.24.0-2ubuntu7.18 Ubuntu:24.04/noble-updates [all])
Conf nginx (1.24.0-2ubuntu7.18 Ubuntu:24.04/noble-updates [amd64])
"""


@pytest.fixture
def unprivileged_test_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run fixed-path logic with temporary fixtures and mocked host commands."""

    monkeypatch.setattr(web_host, "_require_root", lambda: None)
    monkeypatch.setattr(web_host, "_require_root_owned", lambda _info, _label: None)


def _receipt_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], bytes]:
    web_root = tmp_path / "web"
    receipts = tmp_path / "state" / "installed" / web_host.WEB_COMPONENT_ID
    pointer_id = "preview-" + "a" * 32
    release_identity = _sha(b"manifest")
    index = b"<!doctype html><title>Cyrene</title>\n"
    release_dir = web_root / "releases" / pointer_id
    release_dir.mkdir(parents=True)
    (release_dir / "index.html").write_bytes(index)
    (release_dir / "index.html").chmod(0o644)
    (web_root / "current").symlink_to(f"releases/{pointer_id}")
    receipt_dir = receipts / "releases"
    receipt_dir.mkdir(parents=True)
    active = {
        "schemaVersion": 1,
        "componentId": web_host.WEB_COMPONENT_ID,
        "releaseIdentity": release_identity,
        "bundleIdentity": "bundle-identity",
    }
    manifest = {
        "componentId": web_host.WEB_COMPONENT_ID,
        "version": "0.0.1",
        "artifact": {
            "kind": "static-web",
            "entrypoint": "index.html",
            "files": {"index.html": _sha(index)},
        },
    }
    release = {
        "schemaVersion": 2,
        "componentId": web_host.WEB_COMPONENT_ID,
        "releaseIdentity": release_identity,
        "manifestDigest": release_identity,
        "bundleIdentity": "bundle-identity",
        "version": "0.0.1",
        "pointerIdentity": pointer_id,
        "releasePath": str(release_dir),
        "manifest": manifest,
    }
    active_raw = json.dumps(active, separators=(",", ":")).encode()
    release_raw = json.dumps(release, separators=(",", ":")).encode()
    (receipts / "active.json").write_bytes(active_raw)
    (receipt_dir / f"{release_identity.removeprefix('sha256:')}.json").write_bytes(release_raw)
    (receipts / "active.json").chmod(0o600)
    (receipt_dir / f"{release_identity.removeprefix('sha256:')}.json").chmod(0o600)
    monkeypatch.setattr(web_host, "WEB_ROOT", web_root)
    monkeypatch.setattr(web_host, "WEB_CURRENT", web_root / "current")
    monkeypatch.setattr(web_host, "INSTALLED_RECEIPTS", receipts)
    return release, index


def test_nginx_contract_is_loopback_only_and_preserves_same_origin_control_headers() -> None:
    config = web_host.NGINX_CONFIG.decode()

    assert "listen 127.0.0.1:8100;" in config
    assert "listen 0.0.0.0" not in config
    assert "listen 80;" not in config
    assert "root /opt/cyrene/workloads/web/cyrene-client-workspace-web/current;" in config
    assert "proxy_pass http://127.0.0.1:5182;" in config
    for header in (
        "Host $http_host",
        "Origin $http_origin",
        "Cookie $http_cookie",
        "X-Studio-Control-Token $http_x_studio_control_token",
        "X-CSRF-Token $http_x_csrf_token",
    ):
        assert f"proxy_set_header {header};" in config
    assert 'X-Forwarded-For ""' in config


def test_active_receipt_requires_current_pointer_and_manifest_index_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unprivileged_test_hooks: None
) -> None:
    _release, index = _receipt_fixture(tmp_path, monkeypatch)

    source = web_host._load_active_web_receipt()

    assert source["indexDigest"] == _sha(index)
    assert source["sourceReceiptDigest"].startswith("sha256:")
    assert source["indexBytes"] == index


def test_active_receipt_rejects_pointer_or_content_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unprivileged_test_hooks: None
) -> None:
    _release, _index = _receipt_fixture(tmp_path, monkeypatch)
    (web_host.WEB_CURRENT).unlink()
    web_host.WEB_CURRENT.symlink_to("releases/unreceipted")

    with pytest.raises(web_host.WorkloadWebHostError, match="differs from its active receipt"):
        web_host._load_active_web_receipt()


def test_active_receipt_rejects_modified_index_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unprivileged_test_hooks: None
) -> None:
    _release, _index = _receipt_fixture(tmp_path, monkeypatch)
    (web_host.WEB_CURRENT / "index.html").write_bytes(b"modified")

    with pytest.raises(web_host.WorkloadWebHostError, match="index digest"):
        web_host._load_active_web_receipt()


def test_managed_snapshot_refuses_unowned_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nginx.conf"
    path.write_text("server { listen 8100; }\n")
    path.chmod(0o600)
    monkeypatch.setattr(web_host, "_require_root_owned", lambda _info, _label: None)

    with pytest.raises(web_host.WorkloadWebHostError, match="unmanaged"):
        web_host._managed_snapshot(path, web_host.CONFIG_MARKER, "Nginx configuration")


def test_snapshot_validation_rejects_changed_digest() -> None:
    snapshot = {
        "schemaVersion": 1,
        "componentId": web_host.WEB_COMPONENT_ID,
        "configBytes": "Y29uZmln",
        "unitBytes": None,
        "configDigest": _sha(b"config"),
        "unitDigest": None,
        "serviceState": "inactive",
        "serviceEnabled": False,
    }

    with pytest.raises(web_host.WorkloadWebHostError, match="snapshot digest"):
        web_host._decode_snapshot(snapshot)


def test_apt_candidate_must_come_from_official_noble_archive() -> None:
    candidate, sources = web_host._apt_policy_candidate(APT_POLICY)

    assert candidate == "1.24.0-2ubuntu7.18"
    assert sources == [
        {
            "host": "archive.ubuntu.com",
            "suite": "noble-updates",
            "component": "main",
            "architecture": "amd64",
        }
    ]
    with pytest.raises(web_host.WorkloadWebHostError, match="official Ubuntu Noble archive"):
        web_host._apt_policy_candidate(
            APT_POLICY.replace("archive.ubuntu.com", "ppa.example.invalid")
        )


def test_missing_apt_package_is_reported_without_install_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(web_host, "_require_root", lambda: None)
    monkeypatch.setattr(web_host, "_check_supported_host", lambda: None)
    monkeypatch.setattr(web_host, "_installed_package_version", lambda _runner: None)

    def apt_runner(
        arguments: list[str], _environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return _result(arguments, stdout=APT_POLICY)

    result = web_host.prepare_host_prerequisites(
        install_authorized=False,
        runner=apt_runner,
    )

    assert result["ready"] is False
    assert result["requiresAuthorization"] is True
    assert result["package"] == "nginx"
    assert calls == [[str(web_host.APT_CACHE), "policy", "nginx"]]


def test_apt_install_masks_only_new_default_unit_and_records_package_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nginx_binary = tmp_path / "nginx"
    mime_types = tmp_path / "mime.types"
    package_unit = tmp_path / "lib" / "systemd" / "system" / "nginx.service"
    runtime_mask = tmp_path / "run" / "systemd" / "system" / "nginx.service"
    for path in (nginx_binary, mime_types):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    package_unit.parent.mkdir(parents=True)
    package_unit.write_text("fixture unit\n")
    monkeypatch.setattr(web_host, "_require_root", lambda: None)
    monkeypatch.setattr(web_host, "_check_supported_host", lambda: None)
    monkeypatch.setattr(web_host, "NGINX", nginx_binary)
    monkeypatch.setattr(web_host, "NGINX_MIME_TYPES", mime_types)
    monkeypatch.setattr(web_host, "GLOBAL_NGINX_UNIT_PATHS", (tmp_path / "etc-nginx.service",))
    monkeypatch.setattr(web_host, "RUNTIME_NGINX_UNIT_MASK", runtime_mask)
    monkeypatch.setattr(web_host, "PACKAGE_NGINX_UNIT", package_unit)

    installed = False
    disabled = False
    commands: list[list[str]] = []

    def apt_runner(
        arguments: list[str], _environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        nonlocal installed, disabled
        commands.append(arguments)
        if arguments[:3] == [
            str(web_host.DPKG_QUERY),
            "-W",
            "-f=${db:Status-Abbrev}|${Version}\\n",
        ]:
            if installed:
                return _result(arguments, stdout="ii |1.24.0-2ubuntu7.18" + chr(10))
            return _result(arguments, returncode=1)
        if arguments[:3] == [str(web_host.APT_CACHE), "policy", "nginx"]:
            return _result(arguments, stdout=APT_POLICY)
        if arguments[:2] == [str(web_host.APT_GET), "-s"]:
            return _result(arguments, stdout=APT_SIMULATION)
        if arguments[:2] == [str(web_host.SYSTEMCTL), "mask"]:
            runtime_mask.parent.mkdir(parents=True, exist_ok=True)
            runtime_mask.symlink_to("/dev/null")
            return _result(arguments)
        if arguments[:2] == [str(web_host.SYSTEMCTL), "unmask"]:
            runtime_mask.unlink()
            return _result(arguments)
        if arguments[:2] == [str(web_host.APT_GET), "--yes"]:
            installed = True
            return _result(arguments)
        if arguments[:2] == [str(web_host.SYSTEMCTL), "show"]:
            property_name = arguments[2].split("=", 1)[1]
            if property_name == "LoadState":
                return _result(arguments, stdout="loaded\n" if installed else "not-found\n")
            if property_name == "ActiveState":
                return _result(arguments, stdout="inactive\n")
            if property_name == "FragmentPath":
                return _result(arguments, stdout="/lib/systemd/system/nginx.service" + chr(10))
        if arguments[:2] == [str(web_host.SYSTEMCTL), "is-enabled"]:
            return _result(
                arguments,
                stdout="enabled\n" if installed and not disabled else "disabled\n",
                returncode=0 if installed and not disabled else 1,
            )
        if arguments[:2] == [str(web_host.DPKG_QUERY), "-S"]:
            return _result(
                arguments,
                stdout="nginx-core: /lib/systemd/system/nginx.service" + chr(10),
            )
        if arguments[:2] == [str(web_host.SYSTEMCTL), "disable"]:
            disabled = True
            return _result(arguments)
        if arguments[:2] == [str(nginx_binary), "-v"]:
            return _result(arguments, stderr="nginx/1.24.0\n")
        return _result(arguments)

    events: list[dict[str, Any]] = []
    result = web_host.prepare_host_prerequisites(
        install_authorized=True,
        durable_callback=lambda event: events.append(dict(event)),
        runner=apt_runner,
    )

    assert result["ready"] is True
    assert result["installedByThisOperation"] is True
    assert result["packageVersion"] == "1.24.0-2ubuntu7.18"
    assert result["aptSource"][0]["host"] == "archive.ubuntu.com"
    assert result["newDefaultServiceDisabled"] is True
    assert not runtime_mask.exists()
    assert disabled is True
    assert any(command[:2] == [str(web_host.SYSTEMCTL), "mask"] for command in commands)
    assert [event["phase"] for event in events] == ["prepared", "installed"]


def test_browser_route_probe_checks_local_session_without_credentials_or_leaking_csrf_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = b"verified index"
    source = {"indexDigest": _sha(index)}
    calls: list[tuple[str, dict[str, str]]] = []
    session = {
        "authenticated": True,
        "state": "AUTHENTICATED",
        "sessionId": "local",
        "expiresAt": None,
        "refreshExpiresAt": None,
        "refreshable": False,
        "csrfToken": "a" * 64,
        "refreshed": False,
    }

    def get(
        url: str, *, headers: dict[str, str] | None = None, timeout: float = 3.0
    ) -> tuple[int, bytes, str]:
        del timeout
        request_headers = dict(headers or {})
        calls.append((url, request_headers))
        if url == f"{web_host.PUBLIC_ORIGIN}/":
            return 200, index, "text/html"
        if url == f"{web_host.PUBLIC_ORIGIN}/healthz":
            return 200, b'{"status":"ok"}', "application/json"
        if url.endswith("/health/ready"):
            return 200, b'{"status":"ready"}', "application/json"
        return 200, json.dumps(session).encode(), "application/json"

    monkeypatch.setattr(web_host, "_http_get", get)

    probes = web_host._probe_host("nginx/1.24.0", source)

    session_request = next(call for call in calls if call[0].endswith("/api/v1/auth/session"))
    assert session_request[1]["Host"] == "127.0.0.1:8100"
    assert session_request[1]["Origin"] == web_host.PUBLIC_ORIGIN
    assert "Cookie" not in session_request[1]
    assert "Authorization" not in session_request[1]
    assert probes["root"]["indexDigest"] == source["indexDigest"]
    assert probes["localSession"] == {
        "status": 200,
        "authenticated": True,
        "state": "AUTHENTICATED",
        "refreshable": False,
    }
    assert "a" * 64 not in json.dumps(probes)


def test_local_session_probe_does_not_require_workload_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = b"verified index"
    source = {"indexDigest": _sha(index)}

    def get(
        url: str, *, headers: dict[str, str] | None = None, timeout: float = 3.0
    ) -> tuple[int, bytes, str]:
        del headers, timeout
        if url == f"{web_host.PUBLIC_ORIGIN}/":
            return 200, index, "text/html"
        if url == f"{web_host.PUBLIC_ORIGIN}/healthz":
            return 200, b'{"status":"ok"}', "application/json"
        if url.endswith("/health/ready"):
            return 200, b'{"status":"ready"}', "application/json"
        return (
            200,
            b'{"authenticated":true,"state":"AUTHENTICATED","refreshable":false}',
            "application/json",
        )

    monkeypatch.setattr(web_host, "_http_get", get)

    probes = web_host._probe_host("nginx/1.24.0", source)

    assert probes["localSession"]["authenticated"] is True


def test_local_session_probe_rejects_unauthenticated_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = b"verified index"
    source = {"indexDigest": _sha(index)}

    def get(
        url: str, *, headers: dict[str, str] | None = None, timeout: float = 3.0
    ) -> tuple[int, bytes, str]:
        del headers, timeout
        if url == f"{web_host.PUBLIC_ORIGIN}/":
            return 200, index, "text/html"
        if url == f"{web_host.PUBLIC_ORIGIN}/healthz":
            return 200, b'{"status":"ok"}', "application/json"
        if url.endswith("/health/ready"):
            return 200, b'{"status":"ready"}', "application/json"
        return 200, b'{"authenticated":false}', "application/json"

    monkeypatch.setattr(web_host, "_http_get", get)

    with pytest.raises(web_host.WorkloadWebHostError, match="local Control session"):
        web_host._probe_host("nginx/1.24.0", source)


def test_remove_unlinks_only_managed_files_and_preserves_other_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "etc" / "cyrene" / "workloads" / "web"
    unit_dir = tmp_path / "etc" / "systemd" / "system"
    config_dir.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    config_path = config_dir / "nginx.conf"
    unit_path = unit_dir / web_host.HOST_UNIT
    config_path.write_bytes(web_host.NGINX_CONFIG)
    unit_path.write_bytes(web_host.SYSTEMD_UNIT)
    config_path.chmod(0o600)
    unit_path.chmod(0o600)
    data_file = tmp_path / "user-data.json"
    data_file.write_text('{"preserve":true}')
    monkeypatch.setattr(web_host, "_require_root", lambda: None)
    monkeypatch.setattr(web_host, "_require_root_owned", lambda _info, _label: None)
    monkeypatch.setattr(web_host, "HOST_CONFIG_PATH", config_path)
    monkeypatch.setattr(web_host, "HOST_UNIT_PATH", unit_path)
    monkeypatch.setattr(web_host, "HOST_CONFIG_DIRECTORY", config_dir)

    prior = web_host.capture_web_host_state(runner=_fake_runner)
    events: list[dict[str, Any]] = []
    removed = web_host.remove_web_host(
        expected_state=prior,
        durable_callback=lambda event: events.append(dict(event)),
        runner=_fake_runner,
    )

    assert removed["removed"] is True
    assert removed["dataPreserved"] is True
    assert not config_path.exists()
    assert not unit_path.exists()
    assert data_file.read_text() == '{"preserve":true}'
    assert [event["phase"] for event in events] == ["remove-prepared", "removed"]


def test_apply_records_each_durable_phase_and_returns_real_listener_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_host, "_require_root", lambda: None)
    prior = {
        "schemaVersion": 1,
        "componentId": web_host.WEB_COMPONENT_ID,
        "configBytes": None,
        "unitBytes": None,
        "configDigest": None,
        "unitDigest": None,
        "serviceState": "inactive",
        "serviceEnabled": False,
    }
    source = {
        "componentId": web_host.WEB_COMPONENT_ID,
        "version": "0.0.1",
        "releaseIdentity": _sha(b"manifest"),
        "sourceReceiptDigest": _sha(b"release receipt"),
        "activeReceiptDigest": _sha(b"active receipt"),
        "indexDigest": _sha(b"index"),
    }
    events: list[dict[str, Any]] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(web_host, "_load_active_web_receipt", lambda: source)
    monkeypatch.setattr(web_host, "capture_web_host_state", lambda *, runner: prior)
    monkeypatch.setattr(web_host, "_check_host_prerequisites", lambda _runner: "nginx/1.24.0")
    monkeypatch.setattr(web_host, "_port_is_open", lambda: False)
    monkeypatch.setattr(web_host, "_atomic_write", lambda path, payload, mode=0o644: None)
    monkeypatch.setattr(
        web_host,
        "_probe_host",
        lambda version, _source: {
            "nginxVersion": version,
            "root": {"status": 200, "indexDigest": source["indexDigest"]},
            "localSession": {"status": 200},
        },
    )

    def runner(
        arguments: list[str], _environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return _result(arguments)

    receipt = web_host.apply_web_host(
        expected_state=prior,
        durable_callback=lambda event: events.append(dict(event)),
        runner=runner,
    )

    assert receipt["clientUrl"] == "http://127.0.0.1:8100/"
    assert receipt["listenerAddress"] == "127.0.0.1"
    assert receipt["listenerPort"] == 8100
    assert receipt["documentRoot"] == str(web_host.WEB_CURRENT)
    assert receipt["serviceUnit"] == "cyrene-workspace-web.service"
    assert [event["phase"] for event in events] == ["prepared", "configured", "started", "healthy"]
    assert all("token" not in json.dumps(event) for event in events)
    assert [command[1] for command in commands] == ["-t", "daemon-reload", "enable", "start"]


def test_apply_failure_invokes_exact_transaction_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_host, "_require_root", lambda: None)
    prior = {
        "schemaVersion": 1,
        "componentId": web_host.WEB_COMPONENT_ID,
        "configBytes": None,
        "unitBytes": None,
        "configDigest": None,
        "unitDigest": None,
        "serviceState": "inactive",
        "serviceEnabled": False,
    }
    source = {
        "componentId": web_host.WEB_COMPONENT_ID,
        "version": "0.0.1",
        "releaseIdentity": _sha(b"manifest"),
        "sourceReceiptDigest": _sha(b"release receipt"),
        "activeReceiptDigest": _sha(b"active receipt"),
        "indexDigest": _sha(b"index"),
    }
    restored: list[str | None] = []
    monkeypatch.setattr(web_host, "_load_active_web_receipt", lambda: source)
    monkeypatch.setattr(web_host, "capture_web_host_state", lambda *, runner: prior)
    monkeypatch.setattr(web_host, "_check_host_prerequisites", lambda _runner: "nginx/1.24.0")
    monkeypatch.setattr(web_host, "_port_is_open", lambda: False)
    monkeypatch.setattr(web_host, "_atomic_write", lambda path, payload, mode=0o644: None)
    monkeypatch.setattr(
        web_host, "_probe_host", lambda *_args: (_ for _ in ()).throw(RuntimeError("probe failed"))
    )
    monkeypatch.setattr(
        web_host,
        "_restore_snapshot",
        lambda state, *, runner, expected_current_digest=None: restored.append(
            expected_current_digest
        ),
    )

    with pytest.raises(web_host.WorkloadWebHostError, match="prior state was restored"):
        web_host.apply_web_host(expected_state=prior, durable_callback=lambda _event: None)

    assert restored == [_sha(web_host.NGINX_CONFIG)]


def test_read_status_checks_receipt_and_returns_real_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "schemaVersion": 1,
        "componentId": web_host.WEB_COMPONENT_ID,
        "configBytes": base64.b64encode(web_host.NGINX_CONFIG).decode(),
        "unitBytes": base64.b64encode(web_host.SYSTEMD_UNIT).decode(),
        "configDigest": _sha(web_host.NGINX_CONFIG),
        "unitDigest": _sha(web_host.SYSTEMD_UNIT),
        "serviceState": "active",
        "serviceEnabled": True,
    }
    source = {
        "componentId": web_host.WEB_COMPONENT_ID,
        "version": "0.0.1",
        "releaseIdentity": _sha(b"manifest"),
        "sourceReceiptDigest": _sha(b"release receipt"),
        "activeReceiptDigest": _sha(b"active receipt"),
        "indexDigest": _sha(b"index"),
    }
    probes = {
        "nginxVersion": "nginx/1.24.0",
        "root": {"status": 200},
        "localSession": {"status": 200},
    }
    monkeypatch.setattr(web_host, "capture_web_host_state", lambda *, runner: state)
    monkeypatch.setattr(web_host, "_load_active_web_receipt", lambda: source)
    monkeypatch.setattr(web_host, "_probe_host", lambda _version, _source: probes)

    status = web_host.read_web_host_status(
        expected_source_receipt={
            "componentId": web_host.WEB_COMPONENT_ID,
            "releaseIdentity": source["releaseIdentity"],
            "sourceReceiptDigest": source["sourceReceiptDigest"],
        },
        runner=_fake_runner,
    )

    assert status["installed"] is True
    assert status["available"] is True
    assert status["clientUrl"] == "http://127.0.0.1:8100/"
    assert status["probes"] == probes


def test_status_rejects_wrong_component_without_probing() -> None:
    with pytest.raises(
        web_host.WorkloadWebHostError, match="Only the official Workspace web component"
    ):
        web_host.read_web_host_status(component_id="other-component")
