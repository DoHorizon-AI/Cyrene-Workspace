"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_host_fronted_canary.py                                          │
│  Module: tooling.deploy.teams_cloud.tests.test_host_fronted_canary       │
│  Role: Verify read-only host-Caddy canary gates using sanitized fixtures.│
│                                                                          │
│  模块职责：使用去标识 fixture 验证宿主机 Caddy canary 只读门禁。        │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
canary = __import__("host_fronted_canary")

HOST = "bot.example.test"
CA_PATH = "/var/lib/cyrene/staging-root.crt"
IMAGE_ID = "sha256:" + "a" * 64


def _proxy(
    dial: str,
    *,
    transport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one sanitized Caddy reverse-proxy handler."""

    handler: dict[str, Any] = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": dial}],
    }
    if transport is not None:
        handler["transport"] = transport
    return handler


def _adapted(
    *,
    candidate: bool,
    unrelated_dial: str = "other-service:8080",
    ca_path: str = CA_PATH,
) -> dict[str, Any]:
    """Return a small Caddy adaptation tree with one callback and one untouched front."""

    callback = _proxy(
        "127.0.0.1:18443" if candidate else "127.0.0.1:13978",
        transport=(
            {
                "protocol": "http",
                "tls": {
                    "server_name": HOST,
                    "ca": {"provider": "file", "pem_files": [ca_path]},
                },
            }
            if candidate
            else None
        ),
    )
    return {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "listen": [":443"],
                        "routes": [
                            {
                                "match": [{"host": [HOST]}],
                                "handle": [
                                    {
                                        "handler": "subroute",
                                        "routes": [
                                            {
                                                "match": [
                                                    {
                                                        "path": ["/api/messages"],
                                                        "method": ["POST"],
                                                    }
                                                ],
                                                "handle": [callback],
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "match": [{"host": ["other.example.test"]}],
                                "handle": [_proxy(unrelated_dial)],
                            },
                        ],
                    }
                }
            }
        }
    }


def test_caddy_compare_allows_only_callback_upstream_and_transport() -> None:
    """Accept the intended callback TLS route while preserving every other route."""

    hostname, hosts = canary._verify_candidate_caddy(
        source=_adapted(candidate=False),
        candidate=_adapted(candidate=True),
        backend_ca_file=Path(CA_PATH),
    )

    assert hostname == HOST
    assert hosts == (HOST,)


def test_caddy_compare_rejects_any_other_front_change() -> None:
    """Detect changes to a neighboring reverse proxy even if the callback itself is valid."""

    with pytest.raises(canary.CanaryGateError, match="outside the callback"):
        canary._verify_candidate_caddy(
            source=_adapted(candidate=False),
            candidate=_adapted(candidate=True, unrelated_dial="changed.example.test:8080"),
            backend_ca_file=Path(CA_PATH),
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("upstream", "upstream must be only loopback dial"),
        ("sni", "callback hostname as SNI"),
        ("ca", "trust only the selected public CA file"),
        ("extra_ca", "trust only the selected public CA file"),
        ("wrong_trust_provider", "trust only the selected public CA file"),
        ("extra_tls_option", "outside the CA/SNI allowlist"),
        ("except_ports", "outside the CA/SNI allowlist"),
        ("extra_transport_option", "only HTTP protocol and TLS"),
        ("extra_upstream_option", "upstream must be only loopback dial"),
    ],
)
def test_caddy_compare_rejects_unsafe_backend_transport(change: str, message: str) -> None:
    """Require fixed loopback, matching SNI, selected CA, and certificate verification."""

    source = _adapted(candidate=False)
    candidate = _adapted(candidate=True)
    callback = candidate["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"][0]["routes"][0][
        "handle"
    ][0]
    if change == "upstream":
        callback["upstreams"][0]["dial"] = "127.0.0.1:18444"
    elif change == "sni":
        callback["transport"]["tls"]["server_name"] = "wrong.example.test"
    elif change == "ca":
        callback["transport"]["tls"]["ca"]["pem_files"] = ["/tmp/other-ca.pem"]
    elif change == "extra_ca":
        callback["transport"]["tls"]["ca"]["pem_files"].append("/tmp/other-ca.pem")
    elif change == "wrong_trust_provider":
        callback["transport"]["tls"]["ca"]["provider"] = "system"
    elif change == "extra_tls_option":
        callback["transport"]["tls"]["insecure_skip_verify"] = True
    elif change == "except_ports":
        callback["transport"]["tls"]["except_ports"] = ["18443"]
    elif change == "extra_transport_option":
        callback["transport"]["keepalive"] = "off"
    elif change == "extra_upstream_option":
        callback["upstreams"][0]["max_requests"] = 1

    with pytest.raises(canary.CanaryGateError, match=message):
        canary._verify_candidate_caddy(
            source=source,
            candidate=candidate,
            backend_ca_file=Path(CA_PATH),
        )


def test_caddy_compare_rejects_missing_or_ambiguous_callback_routes() -> None:
    """Require one and only one callback proxy with an exact method and path matcher."""

    candidate = _adapted(candidate=True)
    candidate["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"][0]["routes"][0]["match"][0][
        "method"
    ] = ["GET"]
    with pytest.raises(canary.CanaryGateError, match="exactly one POST callback"):
        canary._verify_candidate_caddy(
            source=_adapted(candidate=False),
            candidate=candidate,
            backend_ca_file=Path(CA_PATH),
        )


def test_caddy_or_branch_cannot_widen_callback_method() -> None:
    """Reject an OR branch that would forward a callback path with no POST restriction."""

    candidate = _adapted(candidate=True)
    candidate["apps"]["http"]["servers"]["srv0"]["routes"][0]["handle"][0]["routes"][0]["match"] = [
        {"path": ["/api/messages"], "method": ["POST"]},
        {"path": ["/api/messages"]},
    ]
    with pytest.raises(canary.CanaryGateError, match="exactly one POST callback"):
        canary._verify_candidate_caddy(
            source=_adapted(candidate=False),
            candidate=candidate,
            backend_ca_file=Path(CA_PATH),
        )


def test_caddyfile_receipt_binds_the_exact_source_and_candidate_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject a candidate or source file that changed after the preparation receipt."""

    monkeypatch.setattr(canary.preflight, "_private_artifact", lambda _path: True)
    source = tmp_path / "baseline.Caddyfile"
    active = tmp_path / "active.Caddyfile"
    candidate = tmp_path / "candidate.Caddyfile"
    receipt = tmp_path / "receipt.json"
    ca_file = tmp_path / "ca.pem"
    source.write_text("baseline\n", encoding="utf-8")
    active.write_text("baseline\n", encoding="utf-8")
    candidate.write_text("candidate\n", encoding="utf-8")
    ca_file.write_text("public ca\n", encoding="utf-8")
    receipt.write_text(
        json.dumps(
            {
                "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "candidateSha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                "activeConfigChanged": False,
                "otherProxyRoutesChanged": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        canary,
        "_adapt_caddyfile",
        lambda _binary, path: _adapted(
            candidate=path == candidate,
            ca_path=str(ca_file),
        ),
    )
    monkeypatch.setattr(canary, "_verify_live_caddy_process", lambda **_kwargs: None)
    monkeypatch.setattr(canary, "_read_live_caddy_config", lambda: _adapted(candidate=False))

    hostname, source_hash, candidate_hash = canary._validate_caddy_inputs(
        active_file=active,
        source_file=source,
        candidate_file=candidate,
        receipt_file=receipt,
        backend_ca_file=ca_file,
        caddy_binary="caddy",
    )
    assert hostname == HOST
    assert source_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert candidate_hash == hashlib.sha256(candidate.read_bytes()).hexdigest()

    active.write_text("active configuration changed\n", encoding="utf-8")
    with pytest.raises(canary.CanaryGateError, match="does not match its unchanged-source receipt"):
        canary._validate_caddy_inputs(
            active_file=active,
            source_file=source,
            candidate_file=candidate,
            receipt_file=receipt,
            backend_ca_file=ca_file,
            caddy_binary="caddy",
        )
    active.write_text("baseline\n", encoding="utf-8")

    candidate.write_text("changed after receipt\n", encoding="utf-8")
    with pytest.raises(canary.CanaryGateError, match="does not match its unchanged-source receipt"):
        canary._validate_caddy_inputs(
            active_file=active,
            source_file=source,
            candidate_file=candidate,
            receipt_file=receipt,
            backend_ca_file=ca_file,
            caddy_binary="caddy",
        )


def test_live_caddy_admin_readback_must_equal_active_file_adaptation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Refuse a valid candidate if Caddy's in-memory config differs from its active file."""

    monkeypatch.setattr(canary.preflight, "_private_artifact", lambda _path: True)
    source = tmp_path / "baseline.Caddyfile"
    active = tmp_path / "active.Caddyfile"
    candidate = tmp_path / "candidate.Caddyfile"
    receipt = tmp_path / "receipt.json"
    ca_file = tmp_path / "ca.pem"
    source.write_text("baseline\n", encoding="utf-8")
    active.write_text("baseline\n", encoding="utf-8")
    candidate.write_text("candidate\n", encoding="utf-8")
    ca_file.write_text("public ca\n", encoding="utf-8")
    receipt.write_text(
        json.dumps(
            {
                "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "candidateSha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                "activeConfigChanged": False,
                "otherProxyRoutesChanged": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        canary,
        "_adapt_caddyfile",
        lambda _binary, path: _adapted(candidate=path == candidate, ca_path=str(ca_file)),
    )
    monkeypatch.setattr(canary, "_verify_live_caddy_process", lambda **_kwargs: None)
    monkeypatch.setattr(canary, "_read_live_caddy_config", lambda: {"apps": {}})

    with pytest.raises(canary.CanaryGateError, match="admin configuration differs"):
        canary._validate_caddy_inputs(
            active_file=active,
            source_file=source,
            candidate_file=candidate,
            receipt_file=receipt,
            backend_ca_file=ca_file,
            caddy_binary="caddy",
        )


def test_live_caddy_process_must_load_fixed_config_without_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Tie the active path to Caddy's systemd MainPID and reject resume mode."""

    binary = tmp_path / "caddy"
    binary.write_bytes(b"caddy binary fixture")
    proc_root = tmp_path / "proc"
    process_root = proc_root / "4321"
    process_root.mkdir(parents=True)
    (process_root / "exe").symlink_to(binary)
    commandline = process_root / "cmdline"
    command = [
        str(binary),
        "run",
        "--environ",
        "--config",
        str(canary.ACTIVE_CADDYFILE),
    ]
    commandline.write_bytes(b"\0".join(value.encode() for value in command) + b"\0")

    def checked(argv: list[str], _label: str, **_kwargs: Any) -> str:
        if argv == ["systemctl", "is-active", "--quiet", canary.CADDY_SYSTEMD_UNIT]:
            return ""
        if argv == [
            "systemctl",
            "show",
            canary.CADDY_SYSTEMD_UNIT,
            "--property=MainPID",
            "--value",
        ]:
            return "4321\n"
        raise AssertionError("unexpected systemd read command")

    monkeypatch.setattr(canary, "_checked_command", checked)
    canary._verify_live_caddy_process(
        active_file=canary.ACTIVE_CADDYFILE,
        caddy_binary=str(binary),
        proc_root=proc_root,
    )

    commandline.write_bytes(b"\0".join(value.encode() for value in [*command, "--resume"]) + b"\0")
    with pytest.raises(canary.CanaryGateError, match="does not load the selected file"):
        canary._verify_live_caddy_process(
            active_file=canary.ACTIVE_CADDYFILE,
            caddy_binary=str(binary),
            proc_root=proc_root,
        )

    with pytest.raises(canary.CanaryGateError, match="must be /etc/caddy/Caddyfile"):
        canary._verify_live_caddy_process(
            active_file=tmp_path / "arbitrary.Caddyfile",
            caddy_binary=str(binary),
            proc_root=proc_root,
        )


def test_live_caddy_configuration_uses_loopback_read_only_admin_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read active Caddy state through a local GET and never issue an admin mutation."""

    expected = _adapted(candidate=False)
    requests: list[tuple[str, str]] = []

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return json.dumps(expected).encode()

    class Connection:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            assert (host, port) == canary.CADDY_ADMIN_ADDRESS
            assert timeout == 5.0

        def request(self, method: str, path: str) -> None:
            requests.append((method, path))

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(canary.http.client, "HTTPConnection", Connection)
    assert canary._read_live_caddy_config() == expected
    assert requests == [("GET", "/config/")]


@pytest.mark.parametrize("with_ca", [False, True])
def test_tls_probe_enables_certificate_and_hostname_verification(
    monkeypatch: pytest.MonkeyPatch,
    with_ca: bool,
) -> None:
    """Use verified OpenSSL handshakes and never use insecure verification flags."""

    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["capture_output"] is True
        assert kwargs["stdin"] is canary.subprocess.DEVNULL
        assert kwargs["timeout"] <= 20.0
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="Verification: OK\n")

    monkeypatch.setattr(canary.subprocess, "run", run)
    ca_file = Path(CA_PATH) if with_ca else None
    canary._verify_tls_handshake(
        openssl_binary="openssl",
        connect="127.0.0.1:18443" if with_ca else f"{HOST}:443",
        hostname=HOST,
        ca_file=ca_file,
        label="test handshake",
    )
    argv = calls[0]
    assert "-verify_return_error" in argv
    assert "-verify_hostname" in argv
    assert "-servername" in argv
    assert "-verify" in argv
    assert "-CAfile" in argv if with_ca else "-CAfile" not in argv
    assert "-insecure" not in argv


def test_tls_probe_requires_verified_output_even_when_openssl_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not infer certificate verification from a successful TLS process exit alone."""

    monkeypatch.setattr(
        canary.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )
    with pytest.raises(canary.CanaryGateError, match="did not report a verified certificate chain"):
        canary._verify_tls_handshake(
            openssl_binary="openssl",
            connect=f"{HOST}:443",
            hostname=HOST,
            ca_file=None,
            label="test handshake",
        )


def test_backend_ca_must_be_parseable_public_certificate_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject a private key in the trust input and parse the public certificate with OpenSSL."""

    ca_file = tmp_path / "ca.pem"
    ca_file.write_text(
        "-----BEGIN CERTIFICATE-----\npublic-fixture\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def run(argv: list[str], label: str, **_kwargs: Any) -> str:
        calls.append(argv)
        assert label == "backend CA certificate parsing"
        return "X509v3 Basic Constraints: critical\n    CA:TRUE\n"

    monkeypatch.setattr(canary, "_checked_command", run)
    canary._verify_public_ca_file(ca_file, "openssl")
    assert calls == [["openssl", "x509", "-in", str(ca_file), "-noout", "-text"]]

    ca_file.write_text(
        "-----BEGIN CERTIFICATE-----\npublic-fixture\n-----END CERTIFICATE-----\n"
        "-----BEGIN PRIVATE KEY-----\nprivate-fixture\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    with pytest.raises(canary.CanaryGateError, match="public certificates only"):
        canary._verify_public_ca_file(ca_file, "openssl")


def test_stage1_service_health_requires_every_expected_locked_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require healthy running base services and exclude disabled provider profiles."""

    compose = {
        "name": "cyrene-test",
        "services": {
            "navigator": {
                "image": "navigator:test",
                "restart": "unless-stopped",
                "healthcheck": {"test": ["CMD", "true"]},
            },
            "teams-ingress": {
                "image": "teams:test",
                "restart": "unless-stopped",
                "healthcheck": {"test": ["CMD", "true"]},
            },
            "teams-public-ingress": {
                "image": "caddy:test",
                "restart": "unless-stopped",
                "profiles": ["public-ingress"],
            },
            "cloud-management-phase2": {
                "image": "cloud:test",
                "restart": "unless-stopped",
                "profiles": ["cloud-phase2"],
            },
            "directory-migrate": {"image": "directory:test", "restart": "no"},
        },
    }
    ids = ["a" * 12, "b" * 12, "c" * 12]
    image_lock = {
        "images": [
            {"image": "navigator:test", "imageId": "sha256:" + "1" * 64},
            {"image": "teams:test", "imageId": "sha256:" + "2" * 64},
        ],
        "externalImages": {"caddy": {"localImage": "caddy:test", "imageId": "sha256:" + "3" * 64}},
    }
    service_by_id = dict(
        zip(ids, ["navigator", "teams-ingress", "teams-public-ingress"], strict=True)
    )

    def run(argv: list[str], label: str, *, timeout: float = 20.0) -> str:
        assert label
        if "ps" in argv:
            return "\n".join(ids) + "\n"
        assert "inspect" in argv
        rows = []
        for container_id in ids:
            service_name = service_by_id[container_id]
            image = compose["services"][service_name]["image"]
            suffix = {"navigator": "1", "teams-ingress": "2", "teams-public-ingress": "3"}[
                service_name
            ]
            rows.append(
                {
                    "Config": {
                        "Image": image,
                        "Labels": {
                            "com.docker.compose.project": "cyrene-test",
                            "com.docker.compose.service": service_name,
                        },
                    },
                    "Image": "sha256:" + suffix * 64,
                    "State": {
                        "Running": True,
                        **(
                            {"Health": {"Status": "healthy"}}
                            if service_name != "teams-public-ingress"
                            else {}
                        ),
                    },
                }
            )
        return json.dumps(rows)

    monkeypatch.setattr(canary, "_checked_command", run)
    states = canary._verify_service_containers(
        compose=compose,
        image_lock=image_lock,
        docker_prefix=["docker"],
    )
    assert states == {
        "navigator": "healthy",
        "teams-ingress": "healthy",
        "teams-public-ingress": "running",
    }


def _probe_documents(
    script_file: Path, *, status: str, now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a sanitized reviewed probe plan/result pair."""

    plan = {
        "status": "PLAN_REVIEWED_DIRECT_NATIVE_PROBE",
        "imageId": IMAGE_ID,
        "scriptSha256": hashlib.sha256(script_file.read_bytes()).hexdigest(),
        "profileMountOnly": True,
        "businessSecretsMounted": False,
        "toolServers": 0,
        "maxModelPrompts": 1,
        "at": (now - timedelta(seconds=15)).isoformat(),
    }
    result = {
        "probe": canary.ACCEPTED_PROBE_NAME,
        "status": status,
        "configuredModel": "glm-5.3-flash",
        "noPaidTopUpDeclaration": True,
        "initialize": "PASS",
        "protocolVersion": 1,
        "sessionCreated": status == "PASS",
        "exactNonceReply": status == "PASS",
        "childEnvironmentWasAllowlisted": True,
        "childExited": True,
        "mcpConfigRemoved": True,
        "forbiddenClientRequests": 0,
        "dockerSshExitCode": 0 if status == "PASS" else 2,
        "failureCode": None if status == "PASS" else "AUTH_REQUIRED",
        "recordedAt": now.isoformat(),
    }
    return plan, result


def test_codebuddy_probe_pass_requires_nonce_roundtrip_and_locked_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Accept only a fresh successful native inference probe of the exact locked Navigator image."""

    monkeypatch.setattr(canary.preflight, "_private_artifact", lambda _path: True)
    script = tmp_path / "probe.mjs"
    script.write_text("sanitized probe\n", encoding="utf-8")
    now = datetime.now(UTC)
    plan, inner_result = _probe_documents(script, status="PASS", now=now)
    inner_result.pop("dockerSshExitCode")
    canary._verify_acp_probe(
        plan=plan,
        result={"processExitCode": 0, "result": inner_result},
        script_file=script,
        image_lock={"images": [{"service": "navigator", "imageId": IMAGE_ID}]},
        configured_model="glm-5.3-flash",
        now=now,
        maximum_age=timedelta(minutes=30),
    )


def test_codebuddy_auth_required_blocks_canary_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Do not treat initialized ACP as usable until the real authenticated model probe passes."""

    monkeypatch.setattr(canary.preflight, "_private_artifact", lambda _path: True)
    script = tmp_path / "probe.mjs"
    script.write_text("sanitized probe\n", encoding="utf-8")
    now = datetime.now(UTC)
    plan, inner_result = _probe_documents(script, status="AUTH_REQUIRED", now=now)
    with pytest.raises(canary.CanaryGateError, match="AUTH_REQUIRED"):
        canary._verify_acp_probe(
            plan=plan,
            result={"processExitCode": 2, "result": inner_result},
            script_file=script,
            image_lock={"images": [{"service": "navigator", "imageId": IMAGE_ID}]},
            configured_model="glm-5.3-flash",
            now=now,
            maximum_age=timedelta(minutes=30),
        )


def test_codebuddy_probe_must_match_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Do not accept a successful probe for a different model than Navigator will use."""

    monkeypatch.setattr(canary.preflight, "_private_artifact", lambda _path: True)
    script = tmp_path / "probe.mjs"
    script.write_text("sanitized probe\n", encoding="utf-8")
    now = datetime.now(UTC)
    plan, result = _probe_documents(script, status="PASS", now=now)
    with pytest.raises(canary.CanaryGateError, match="has not passed"):
        canary._verify_acp_probe(
            plan=plan,
            result=result,
            script_file=script,
            image_lock={"images": [{"service": "navigator", "imageId": IMAGE_ID}]},
            configured_model="deepseek-v4.1-flash",
            now=now,
            maximum_age=timedelta(minutes=30),
        )


def test_readiness_result_never_means_deployment_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep the canary result explicitly separate from cutover and end-to-end acceptance."""

    monkeypatch.setattr(canary, "_validate_private_inputs", lambda **_kwargs: None)
    ingress_image_id = "sha256:" + "3" * 64
    image_lock = {
        "images": [{"service": "navigator", "imageId": IMAGE_ID}],
        "externalImages": {"caddy": {"localImage": "caddy:test", "imageId": ingress_image_id}},
    }
    plan = {"status": "reviewed"}
    result = {"status": "AUTH_REQUIRED"}
    monkeypatch.setattr(
        canary,
        "_read_json_object",
        lambda _path, label: {
            "image lock": image_lock,
            "ACP probe plan": plan,
            "ACP probe result": result,
        }[label],
    )
    preflight_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        canary.preflight,
        "run_preflight",
        lambda **kwargs: (
            preflight_calls.append(kwargs)
            or {
                "status": "READY",
                "mode": "stage1",
                "deploymentReady": True,
                "ownedStagingIngressImageId": ingress_image_id,
            }
        ),
    )
    monkeypatch.setattr(
        canary.preflight,
        "_compose_config",
        lambda *_args: {
            "name": "cyrene-test",
            "services": {
                "teams-public-ingress": {"image": "caddy:test"},
                "navigator": {"image": "navigator:test"},
            },
        },
    )
    monkeypatch.setattr(
        canary, "_verify_service_containers", lambda **_kwargs: {"navigator": "healthy"}
    )
    monkeypatch.setattr(
        canary, "_validate_caddy_inputs", lambda **_kwargs: (HOST, "a" * 64, "b" * 64)
    )
    monkeypatch.setattr(canary, "_verify_public_ca_file", lambda *_args: None)
    monkeypatch.setattr(canary, "_verify_tls_handshake", lambda **_kwargs: None)
    monkeypatch.setattr(canary, "_verify_acp_probe", lambda **_kwargs: None)

    result = canary.run_canary_gate(
        compose_files=[tmp_path / "compose.yaml"],
        env_files=[tmp_path / "runtime.env"],
        image_lock_path=tmp_path / "image-lock.json",
        secrets_dir=tmp_path / "secrets",
        config_dir=tmp_path / "config",
        source_caddyfile=tmp_path / "baseline.Caddyfile",
        candidate_caddyfile=tmp_path / "candidate.Caddyfile",
        caddy_receipt=tmp_path / "receipt.json",
        backend_ca_file=tmp_path / "ca.pem",
        acp_plan_path=tmp_path / "acp-plan.json",
        acp_result_path=tmp_path / "acp-result.json",
        acp_script_path=tmp_path / "probe.mjs",
        caddy_binary="caddy",
        openssl_binary="openssl",
        docker_prefix=["docker"],
        max_memory_mib=3584,
        max_cpus=1.95,
        required_free_gib=2.0,
        minimum_mem_available_mib=1024,
        maximum_probe_age_seconds=1800,
    )

    assert result["status"] == canary.CANARY_STATUS
    assert result["deploymentReady"] is False
    assert result["accepted"] is False
    assert result["fullTeamsAcceptance"] == "NOT_RUN"
    assert result["cloudProviderAcceptance"] == "NOT_RUN"
    assert result["twentyFourHourObservation"] == "NOT_RUN"
    assert preflight_calls[0]["mode"] == "stage1"
    assert preflight_calls[0]["allow_owned_staging_ingress"] is True
