"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 host_fronted_canary.py                                               │
│  Module: tooling.deploy.teams_cloud.host_fronted_canary                  │
│  Role: Read-only gate for a host-Caddy-fronted Teams callback canary.    │
│                                                                          │
│  模块职责：只读检查宿主机 Caddy 前置 Teams 回调 canary 是否具备条件。   │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import ipaddress
import itertools
import json
import math
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import preflight
from build_arm64 import BuildError

CANARY_STATUS = "CANARY_READY_NOT_ACCEPTED"
CANARY_BLOCKED_STATUS = "CANARY_BLOCKED"
ACCEPTED_PROBE_NAME = "REAL_VM_INTERNAL_DIRECT_CODEBUDDY_ACP"
ACTIVE_CADDYFILE = Path("/etc/caddy/Caddyfile")
CADDY_SYSTEMD_UNIT = "caddy.service"
CADDY_ADMIN_ADDRESS = ("127.0.0.1", 2019)
MAX_CADDY_ADMIN_CONFIG_BYTES = 4 * 1024**2


class CanaryGateError(ValueError):
    """A required read-only canary readiness check failed."""


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read one closed JSON object without exposing malformed input details."""

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=preflight._unique_json_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CanaryGateError(f"{label} is not readable JSON") from error
    if not isinstance(value, dict):
        raise CanaryGateError(f"{label} must be a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a regular input file."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise CanaryGateError("a required canary input could not be hashed") from error
    return digest.hexdigest()


def _require_regular_file(path: Path, label: str, *, private: bool = False) -> None:
    """Require a non-symlink regular file and, when requested, an umbrella artifact."""

    if path.is_symlink() or not path.is_file():
        raise CanaryGateError(f"{label} must be a regular file")
    if private and not preflight._private_artifact(path):
        raise CanaryGateError(f"{label} must be stored under the private umbrella artifacts")


def _checked_command(
    argv: list[str],
    label: str,
    *,
    timeout: float = 20.0,
    include_stderr: bool = False,
) -> str:
    """Run a bounded read-only command and return output only when it succeeds."""

    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CanaryGateError(f"{label} could not be completed") from error
    if result.returncode != 0:
        raise CanaryGateError(f"{label} failed")
    return result.stdout + (result.stderr if include_stderr else "")


def _adapt_caddyfile(caddy_binary: str, caddyfile: Path) -> dict[str, Any]:
    """Ask the installed Caddy binary to adapt a Caddyfile without loading it."""

    output = _checked_command(
        [caddy_binary, "adapt", "--config", str(caddyfile), "--adapter", "caddyfile", "--pretty"],
        "Caddyfile adaptation",
    )
    try:
        adapted = json.loads(output, object_pairs_hook=preflight._unique_json_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise CanaryGateError("Caddy returned invalid adapted JSON") from error
    if not isinstance(adapted, dict):
        raise CanaryGateError("Caddy returned an unsupported adapted configuration")
    return adapted


def _verify_live_caddy_process(
    *,
    active_file: Path,
    caddy_binary: str,
    proc_root: Path = Path("/proc"),
) -> None:
    """Tie the active systemd Caddy process to the fixed Caddyfile path."""

    if active_file.resolve() != ACTIVE_CADDYFILE.resolve():
        raise CanaryGateError("live Caddy configuration must be /etc/caddy/Caddyfile")
    _checked_command(
        ["systemctl", "is-active", "--quiet", CADDY_SYSTEMD_UNIT],
        "Caddy systemd active-state readback",
    )
    pid_text = _checked_command(
        ["systemctl", "show", CADDY_SYSTEMD_UNIT, "--property=MainPID", "--value"],
        "Caddy systemd process readback",
    ).strip()
    if not pid_text.isdigit() or int(pid_text) <= 1:
        raise CanaryGateError("Caddy systemd unit has no active main process")
    process_root = proc_root / pid_text
    try:
        arguments = [
            value.decode("utf-8")
            for value in (process_root / "cmdline").read_bytes().split(b"\0")
            if value
        ]
        process_binary = (process_root / "exe").resolve()
    except (OSError, UnicodeDecodeError) as error:
        raise CanaryGateError("live Caddy process arguments are unreadable") from error
    config_positions = [index for index, value in enumerate(arguments) if value == "--config"]
    if (
        process_binary != Path(caddy_binary).resolve()
        or len(arguments) < 2
        or arguments[1] != "run"
        or any(_is_resume_flag(value) for value in arguments)
        or len(config_positions) != 1
        or config_positions[0] + 1 >= len(arguments)
        or Path(arguments[config_positions[0] + 1]).resolve() != active_file.resolve()
    ):
        raise CanaryGateError("live Caddy process does not load the selected file without --resume")


def _is_resume_flag(argument: str) -> bool:
    """Reject every Caddy CLI spelling of the saved-config resume flag."""

    return argument.partition("=")[0] in {"-r", "-resume", "--resume"}


def _read_live_caddy_config() -> dict[str, Any]:
    """Fetch the active configuration from Caddy's loopback-only read API."""

    connection = http.client.HTTPConnection(*CADDY_ADMIN_ADDRESS, timeout=5.0)
    try:
        connection.request("GET", "/config/")
        response = connection.getresponse()
        if response.status != 200:
            raise CanaryGateError("Caddy admin API did not return its active configuration")
        payload = response.read(MAX_CADDY_ADMIN_CONFIG_BYTES + 1)
        if len(payload) > MAX_CADDY_ADMIN_CONFIG_BYTES:
            raise CanaryGateError("Caddy admin configuration exceeds the read limit")
    except (OSError, http.client.HTTPException) as error:
        raise CanaryGateError("Caddy loopback admin configuration read failed") from error
    finally:
        connection.close()
    try:
        config = json.loads(payload, object_pairs_hook=preflight._unique_json_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise CanaryGateError("Caddy admin API returned invalid configuration JSON") from error
    if not isinstance(config, dict):
        raise CanaryGateError("Caddy admin API configuration is not a JSON object")
    return config


def _route_match_combinations(
    ancestors: tuple[dict[str, Any], ...],
) -> list[tuple[dict[str, Any], ...]]:
    """Expand route matcher OR branches while retaining parent/child AND semantics."""

    route_sets: list[list[dict[str, Any]]] = []
    for ancestor in ancestors:
        if "match" not in ancestor:
            continue
        matchers = ancestor["match"]
        if not isinstance(matchers, list) or not matchers:
            raise CanaryGateError("candidate Caddy contains an empty or malformed route matcher")
        if any(not isinstance(matcher, dict) for matcher in matchers):
            raise CanaryGateError("candidate Caddy contains a malformed matcher set")
        route_sets.append(matchers)
    combination_count = math.prod(len(values) for values in route_sets)
    if combination_count > 256:
        raise CanaryGateError("candidate Caddy has too many matcher branches to verify")
    return [tuple(values) for values in itertools.product(*route_sets)]


def _callback_hosts(ancestors: tuple[dict[str, Any], ...]) -> tuple[str, ...] | None:
    """Prove every inherited OR branch is constrained to the same POST callback host/path."""

    combinations = _route_match_combinations(ancestors)
    if not combinations:
        return None
    common_hosts: set[str] | None = None
    for branch in combinations:
        path_sets = [matcher["path"] for matcher in branch if "path" in matcher]
        method_sets = [matcher["method"] for matcher in branch if "method" in matcher]
        host_sets = [matcher["host"] for matcher in branch if "host" in matcher]
        if (
            not path_sets
            or not method_sets
            or not host_sets
            or any(paths != ["/api/messages"] for paths in path_sets)
            or any(methods != ["POST"] for methods in method_sets)
            or any(
                not isinstance(hosts, list) or len(hosts) != 1 or not isinstance(hosts[0], str)
                for hosts in host_sets
            )
        ):
            return None
        branch_hosts = {hosts[0] for hosts in host_sets}
        if len(branch_hosts) != 1:
            return None
        hostname = next(iter(branch_hosts))
        if not isinstance(hostname, str) or not _valid_dns_name(hostname):
            return None
        if common_hosts is None:
            common_hosts = {hostname}
        elif common_hosts != {hostname}:
            return None
    return tuple(sorted(common_hosts or ()))


def _walk_reverse_proxies(
    value: Any,
    *,
    path: tuple[str | int, ...] = (),
    ancestors: tuple[dict[str, Any], ...] = (),
) -> list[tuple[tuple[str | int, ...], dict[str, Any], tuple[dict[str, Any], ...]]]:
    """Collect reverse-proxy handlers and their route ancestry from Caddy JSON."""

    found: list[tuple[tuple[str | int, ...], dict[str, Any], tuple[dict[str, Any], ...]]] = []
    if isinstance(value, dict):
        if value.get("handler") == "reverse_proxy":
            found.append((path, value, ancestors))
        next_ancestors = (*ancestors, value)
        for key, child in value.items():
            found.extend(
                _walk_reverse_proxies(
                    child,
                    path=(*path, key),
                    ancestors=next_ancestors,
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(
                _walk_reverse_proxies(
                    child,
                    path=(*path, index),
                    ancestors=ancestors,
                )
            )
    return found


def _find_callback_proxy(
    adapted: dict[str, Any], label: str
) -> tuple[tuple[str | int, ...], dict[str, Any], tuple[str, ...]]:
    """Require one reverse proxy beneath one exact POST callback route."""

    matching = []
    for path, handler, ancestors in _walk_reverse_proxies(adapted):
        hosts = _callback_hosts(ancestors)
        if hosts is not None:
            matching.append((path, handler, hosts))
    if len(matching) != 1:
        raise CanaryGateError(f"{label} must contain exactly one POST callback reverse proxy")
    path, handler, hosts = matching[0]
    if len(hosts) != 1 or not _valid_dns_name(hosts[0]):
        raise CanaryGateError(f"{label} callback must bind exactly one public DNS name")
    return path, handler, hosts


def _valid_dns_name(value: str) -> bool:
    """Accept a concrete DNS hostname and reject wildcard/IP site addresses."""

    if not value or value != value.lower() or value.endswith("."):
        return False
    try:
        ipaddress.ip_address(value)
        return False
    except ValueError:
        pass
    if len(value) > 253:
        return False
    labels = value.split(".")
    return len(labels) >= 2 and all(
        1 <= len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is not None
        for label in labels
    )


def _at_path(document: Any, path: tuple[str | int, ...]) -> Any:
    """Return one adapted JSON node by its stable object/list path."""

    value = document
    for component in path:
        value = value[component]
    return value


def _normalized_caddy(adapted: dict[str, Any], path: tuple[str | int, ...]) -> dict[str, Any]:
    """Replace only callback upstream and transport values before full-tree comparison."""

    normalized = copy.deepcopy(adapted)
    handler = _at_path(normalized, path)
    if not isinstance(handler, dict) or handler.get("handler") != "reverse_proxy":
        raise CanaryGateError("adapted callback path does not identify a reverse proxy")
    handler["upstreams"] = "<callback-upstreams>"
    handler["transport"] = "<callback-transport>"
    return normalized


def _verify_candidate_caddy(
    *,
    source: dict[str, Any],
    candidate: dict[str, Any],
    backend_ca_file: Path,
) -> tuple[str, tuple[str, ...]]:
    """Require a single callback backend/TLS change and no other adapted Caddy change."""

    source_path, _source_handler, source_hosts = _find_callback_proxy(source, "current host Caddy")
    candidate_path, candidate_handler, candidate_hosts = _find_callback_proxy(
        candidate, "candidate host Caddy"
    )
    if source_path != candidate_path or source_hosts != candidate_hosts:
        raise CanaryGateError("candidate Caddy changed the callback route placement or hostname")

    upstreams = candidate_handler.get("upstreams")
    if not isinstance(upstreams, list) or len(upstreams) != 1:
        raise CanaryGateError("candidate callback must have exactly one HTTPS upstream")
    upstream = upstreams[0]
    if (
        not isinstance(upstream, dict)
        or set(upstream) != {"dial"}
        or upstream.get("dial") != "127.0.0.1:18443"
    ):
        raise CanaryGateError(
            "candidate callback upstream must be only loopback dial 127.0.0.1:18443"
        )
    transport = candidate_handler.get("transport")
    if (
        not isinstance(transport, dict)
        or set(transport) != {"protocol", "tls"}
        or transport.get("protocol") != "http"
        or not isinstance(transport.get("tls"), dict)
    ):
        raise CanaryGateError(
            "candidate callback transport must contain only HTTP protocol and TLS"
        )
    tls = transport["tls"]
    hostname = candidate_hosts[0]
    if set(tls) != {"ca", "server_name"}:
        raise CanaryGateError("candidate backend TLS options are outside the CA/SNI allowlist")
    if tls.get("server_name") != hostname:
        raise CanaryGateError("candidate backend TLS must use the callback hostname as SNI")
    ca = tls.get("ca")
    if (
        not isinstance(ca, dict)
        or set(ca) != {"pem_files", "provider"}
        or ca.get("provider") != "file"
        or ca.get("pem_files") != [str(backend_ca_file)]
    ):
        raise CanaryGateError("candidate backend TLS must trust only the selected public CA file")

    if _normalized_caddy(source, source_path) != _normalized_caddy(candidate, candidate_path):
        raise CanaryGateError("candidate Caddy changes routes outside the callback upstream/TLS")
    return hostname, source_hosts


def _validate_caddy_inputs(
    *,
    active_file: Path,
    source_file: Path,
    candidate_file: Path,
    receipt_file: Path,
    backend_ca_file: Path,
    caddy_binary: str,
) -> tuple[str, str, str]:
    """Bind adapted Caddy comparison to the private preparation receipt and CA file."""

    _require_regular_file(active_file, "current host Caddyfile")
    for path, label in (
        (source_file, "baseline host Caddyfile"),
        (candidate_file, "candidate host Caddyfile"),
        (receipt_file, "candidate preparation receipt"),
    ):
        _require_regular_file(path, label, private=True)
    _require_regular_file(backend_ca_file, "backend CA certificate")
    receipt = _read_json_object(receipt_file, "candidate preparation receipt")
    source_digest = _sha256_file(source_file)
    active_digest = _sha256_file(active_file)
    candidate_digest = _sha256_file(candidate_file)
    if (
        active_digest != source_digest
        or receipt.get("sourceSha256") != source_digest
        or receipt.get("candidateSha256") != candidate_digest
        or receipt.get("activeConfigChanged") is not False
        or receipt.get("otherProxyRoutesChanged") is not False
    ):
        raise CanaryGateError("candidate Caddyfile does not match its unchanged-source receipt")
    _verify_live_caddy_process(active_file=active_file, caddy_binary=caddy_binary)
    source = _adapt_caddyfile(caddy_binary, source_file)
    candidate = _adapt_caddyfile(caddy_binary, candidate_file)
    if _read_live_caddy_config() != source:
        raise CanaryGateError("live Caddy admin configuration differs from the active Caddyfile")
    if _sha256_file(active_file) != source_digest:
        raise CanaryGateError("active Caddyfile changed while the live configuration was checked")
    _verify_live_caddy_process(active_file=active_file, caddy_binary=caddy_binary)
    hostname, _hosts = _verify_candidate_caddy(
        source=source,
        candidate=candidate,
        backend_ca_file=backend_ca_file,
    )
    return hostname, source_digest, candidate_digest


def _verify_tls_handshake(
    *,
    openssl_binary: str,
    connect: str,
    hostname: str,
    ca_file: Path | None,
    label: str,
) -> None:
    """Verify a TLS peer chain and hostname without sending an HTTP request."""

    argv = [
        openssl_binary,
        "s_client",
        "-connect",
        connect,
        "-servername",
        hostname,
        "-verify_hostname",
        hostname,
        "-verify",
        "5",
        "-verify_return_error",
        "-brief",
    ]
    if ca_file is not None:
        argv.extend(["-CAfile", str(ca_file)])
    output = _checked_command(argv, label, timeout=15.0, include_stderr=True)
    if "Verification: OK" not in output and "Verify return code: 0 (ok)" not in output:
        raise CanaryGateError(f"{label} did not report a verified certificate chain")


def _verify_public_ca_file(ca_file: Path, openssl_binary: str) -> None:
    """Require the selected backend trust file to contain only public certificate data."""

    _require_regular_file(ca_file, "backend CA certificate")
    try:
        contents = ca_file.read_bytes()
    except OSError as error:
        raise CanaryGateError("backend CA certificate could not be read") from error
    if b"-----BEGIN CERTIFICATE-----" not in contents or b"PRIVATE KEY" in contents:
        raise CanaryGateError("backend CA file must contain public certificates only")
    certificate = _checked_command(
        [openssl_binary, "x509", "-in", str(ca_file), "-noout", "-text"],
        "backend CA certificate parsing",
    )
    if "CA:TRUE" not in certificate:
        raise CanaryGateError("backend trust certificate is not a certificate authority")


def _inspect_json(docker_prefix: list[str], container_ids: list[str]) -> list[dict[str, Any]]:
    """Inspect exact container IDs as JSON without logging Docker configuration."""

    if not container_ids:
        return []
    if any(re.fullmatch(r"[0-9a-f]{12,64}", value) is None for value in container_ids):
        raise CanaryGateError("Docker returned an invalid container ID")
    output = _checked_command([*docker_prefix, "inspect", *container_ids], "Docker readback")
    try:
        rows = json.loads(output, object_pairs_hook=preflight._unique_json_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise CanaryGateError("Docker returned invalid inspection JSON") from error
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CanaryGateError("Docker inspection did not return container objects")
    return rows


def _locked_image_ids(image_lock: dict[str, Any]) -> dict[str, str]:
    """Map every locked runtime image reference to its exact local Docker image ID."""

    images: dict[str, str] = {}
    for row in image_lock.get("images", []):
        if isinstance(row, dict) and isinstance(row.get("image"), str):
            image_id = row.get("imageId")
            if (
                not isinstance(image_id, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
            ):
                raise CanaryGateError("image lock contains a malformed local image ID")
            if row["image"] in images:
                raise CanaryGateError("image lock contains duplicate image references")
            images[row["image"]] = image_id
    for row in image_lock.get("externalImages", {}).values():
        if isinstance(row, dict) and isinstance(row.get("localImage"), str):
            image_id = row.get("imageId")
            if (
                not isinstance(image_id, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
            ):
                raise CanaryGateError("image lock contains a malformed external image ID")
            if row["localImage"] in images:
                raise CanaryGateError("image lock contains duplicate image references")
            images[row["localImage"]] = image_id
    return images


def _expected_stage1_services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Select long-running base services plus the public callback profile for Stage 1."""

    services = compose.get("services")
    if not isinstance(services, dict):
        raise CanaryGateError("rendered Compose configuration has no services")
    expected: dict[str, dict[str, Any]] = {}
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, dict):
            raise CanaryGateError("rendered Compose service is malformed")
        if service.get("restart") == "no":
            continue
        profiles = service.get("profiles", [])
        if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
            raise CanaryGateError("rendered Compose service profiles are malformed")
        if profiles and "public-ingress" not in profiles:
            continue
        expected[name] = service
    if "teams-ingress" not in expected or "teams-public-ingress" not in expected:
        raise CanaryGateError("Stage 1 Compose services must include both Teams ingress services")
    return expected


def _container_labels(container: dict[str, Any]) -> dict[str, str]:
    """Return validated Compose labels from one Docker inspection record."""

    config = container.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
    ):
        raise CanaryGateError("Docker container lacks valid Compose labels")
    return labels


def _verify_service_containers(
    *,
    compose: dict[str, Any],
    image_lock: dict[str, Any],
    docker_prefix: list[str],
) -> dict[str, str]:
    """Require every active Stage 1 service to run healthy with its locked image."""

    expected = _expected_stage1_services(compose)
    project = compose.get("name")
    if (
        not isinstance(project, str)
        or not project
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project) is None
    ):
        raise CanaryGateError("rendered Compose project name is missing or invalid")
    image_ids = _locked_image_ids(image_lock)
    project_ids_output = _checked_command(
        [
            *docker_prefix,
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        "running Compose service inventory",
    )
    project_ids = [row.strip() for row in project_ids_output.splitlines() if row.strip()]
    containers = _inspect_json(docker_prefix, project_ids)
    seen: dict[str, dict[str, Any]] = {}
    for container in containers:
        labels = _container_labels(container)
        service_name = labels.get("com.docker.compose.service")
        if labels.get("com.docker.compose.project") != project or not service_name:
            raise CanaryGateError(
                "running container does not belong to the selected Compose project"
            )
        if service_name not in expected:
            raise CanaryGateError("unexpected non-Stage-1 Compose service is running")
        if service_name in seen:
            raise CanaryGateError("a Stage 1 Compose service has multiple running containers")
        seen[service_name] = container

    missing = sorted(set(expected) - set(seen))
    if missing:
        raise CanaryGateError("Stage 1 services are not running: " + ", ".join(missing))

    states: dict[str, str] = {}
    for service_name, service in expected.items():
        container = seen[service_name]
        state = container.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise CanaryGateError(f"Stage 1 service is not running: {service_name}")
        config = container.get("Config")
        runtime_image = config.get("Image") if isinstance(config, dict) else None
        actual_image_id = container.get("Image")
        locked_image = service.get("image")
        if (
            not isinstance(locked_image, str)
            or runtime_image != locked_image
            or image_ids.get(locked_image) != actual_image_id
        ):
            raise CanaryGateError(f"Stage 1 service image does not match the lock: {service_name}")
        if service.get("healthcheck"):
            health = state.get("Health")
            if not isinstance(health, dict) or health.get("Status") != "healthy":
                raise CanaryGateError(f"Stage 1 service is not healthy: {service_name}")
            states[service_name] = "healthy"
        else:
            states[service_name] = "running"
    return states


def _parse_utc_timestamp(value: Any, label: str) -> datetime:
    """Parse an ISO-8601 timestamp and require an explicit UTC offset."""

    if not isinstance(value, str):
        raise CanaryGateError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CanaryGateError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CanaryGateError(f"{label} timestamp must be UTC")
    return parsed.astimezone(UTC)


def _verify_acp_probe(
    *,
    plan: dict[str, Any],
    result: dict[str, Any],
    result_file: Path,
    script_file: Path,
    image_lock: dict[str, Any],
    configured_model: str,
    now: datetime,
    maximum_age: timedelta,
) -> None:
    """Require a fresh successful direct probe of the exact locked Navigator image."""

    nested_result = result.get("result")
    if nested_result is None:
        legacy_flat_result = True
        probe_result = result
        process_exit_code = result.get("dockerSshExitCode")
    elif isinstance(nested_result, dict):
        legacy_flat_result = False
        probe_result = nested_result
        process_exit_code = result.get("processExitCode")
    else:
        raise CanaryGateError("native ACP probe result envelope is malformed")
    if probe_result.get("status") == "AUTH_REQUIRED":
        raise CanaryGateError("official CodeBuddy ACP probe is AUTH_REQUIRED")
    _require_regular_file(result_file, "native ACP probe result", private=True)
    _require_regular_file(script_file, "native ACP probe script", private=True)
    navigator_images = [
        row
        for row in image_lock.get("images", [])
        if isinstance(row, dict) and row.get("service") == "navigator"
    ]
    if len(navigator_images) != 1:
        raise CanaryGateError("image lock must contain exactly one Navigator image")
    expected_image_id = navigator_images[0].get("imageId")
    plan_time = _parse_utc_timestamp(plan.get("at"), "ACP probe plan")
    recorded_at = probe_result.get("recordedAt")
    if recorded_at is None:
        if not legacy_flat_result:
            raise CanaryGateError(
                "native ACP probe wrapper is missing its UTC completion timestamp"
            )
        try:
            recorded_at = datetime.fromtimestamp(result_file.stat().st_mtime, UTC).isoformat()
        except OSError as error:
            raise CanaryGateError("native ACP probe result timestamp is unavailable") from error
    result_time = _parse_utc_timestamp(recorded_at, "ACP probe result")
    age = now.astimezone(UTC) - result_time
    if age < timedelta(0) or age > maximum_age or result_time < plan_time:
        raise CanaryGateError("ACP probe result is stale or predates its reviewed plan")
    if (
        plan.get("status") != "PLAN_REVIEWED_DIRECT_NATIVE_PROBE"
        or plan.get("imageId") != expected_image_id
        or plan.get("scriptSha256") != _sha256_file(script_file)
        or plan.get("profileMountOnly") is not True
        or plan.get("businessSecretsMounted") is not False
        or plan.get("toolServers") != 0
        or plan.get("maxModelPrompts") != 1
        or probe_result.get("probe") != ACCEPTED_PROBE_NAME
        or probe_result.get("status") != "PASS"
        or configured_model not in {"glm-5.3-flash", "deepseek-v4.1-flash"}
        or probe_result.get("configuredModel") != configured_model
        or probe_result.get("noPaidTopUpDeclaration") is not True
        or probe_result.get("initialize") != "PASS"
        or probe_result.get("protocolVersion") != 1
        or probe_result.get("sessionCreated") is not True
        or probe_result.get("exactNonceReply") is not True
        or probe_result.get("childEnvironmentWasAllowlisted") is not True
        or probe_result.get("childExited") is not True
        or probe_result.get("mcpConfigRemoved") is not True
        or probe_result.get("forbiddenClientRequests") != 0
        or isinstance(process_exit_code, bool)
        or process_exit_code != 0
        or probe_result.get("failureCode") not in {None, ""}
    ):
        status = probe_result.get("status")
        if status == "AUTH_REQUIRED":
            raise CanaryGateError("official CodeBuddy ACP probe is AUTH_REQUIRED")
        raise CanaryGateError(
            "official CodeBuddy ACP probe has not passed the real inference checks"
        )


def _validate_private_inputs(
    *,
    image_lock: Path,
    env_files: list[Path],
    source_caddyfile: Path,
    candidate_caddyfile: Path,
    caddy_receipt: Path,
    acp_plan: Path,
    acp_result: Path,
    acp_script: Path,
) -> None:
    """Keep all deployment inventories and probe evidence outside public source trees."""

    paths = [
        image_lock,
        *env_files,
        source_caddyfile,
        candidate_caddyfile,
        caddy_receipt,
        acp_plan,
        acp_result,
        acp_script,
    ]
    for path in paths:
        _require_regular_file(path, "private canary input", private=True)
        if path.stat().st_mode & 0o077:
            raise CanaryGateError("private canary inputs must be owner-only files")


def run_canary_gate(
    *,
    compose_files: list[Path],
    env_files: list[Path],
    image_lock_path: Path,
    secrets_dir: Path,
    config_dir: Path,
    source_caddyfile: Path,
    candidate_caddyfile: Path,
    caddy_receipt: Path,
    backend_ca_file: Path,
    acp_plan_path: Path,
    acp_result_path: Path,
    acp_script_path: Path,
    caddy_binary: str,
    openssl_binary: str,
    docker_prefix: list[str],
    max_memory_mib: int,
    max_cpus: float,
    required_free_gib: float,
    minimum_mem_available_mib: int,
    maximum_probe_age_seconds: int,
) -> dict[str, Any]:
    """Run a read-only canary readiness check without changing deployment state."""

    _validate_private_inputs(
        image_lock=image_lock_path,
        env_files=env_files,
        source_caddyfile=source_caddyfile,
        candidate_caddyfile=candidate_caddyfile,
        caddy_receipt=caddy_receipt,
        acp_plan=acp_plan_path,
        acp_result=acp_result_path,
        acp_script=acp_script_path,
    )
    image_lock = _read_json_object(image_lock_path, "image lock")
    acp_plan = _read_json_object(acp_plan_path, "ACP probe plan")
    acp_result = _read_json_object(acp_result_path, "ACP probe result")
    if maximum_probe_age_seconds < 60 or maximum_probe_age_seconds > 3600:
        raise CanaryGateError("ACP probe maximum age must be between 60 and 3600 seconds")

    # ── Phase 1: Re-run the canonical static and identity preflight ─────
    # 第一阶段：重新运行正式静态配置、范围和身份预检。
    static_preflight = preflight.run_preflight(
        mode="stage1",
        compose_files=compose_files,
        env_files=env_files,
        image_lock=image_lock_path,
        secrets_dir=secrets_dir,
        config_dir=config_dir,
        github_hf_evidence=None,
        azure_gcp_ledger=None,
        runpod_ledger=None,
        azure_gcp_api_evidence=None,
        runpod_api_evidence=None,
        docker_prefix=docker_prefix,
        max_memory_mib=max_memory_mib,
        max_cpus=max_cpus,
        required_free_gib=required_free_gib,
        minimum_mem_available_mib=minimum_mem_available_mib,
        allow_owned_staging_ingress=True,
    )
    if (
        static_preflight.get("mode") != "stage1"
        or static_preflight.get("deploymentReady") is not True
    ):
        raise CanaryGateError("canonical Stage 1 preflight did not return its expected status")
    compose = preflight._compose_config(docker_prefix, compose_files, env_files)
    service_states = _verify_service_containers(
        compose=compose,
        image_lock=image_lock,
        docker_prefix=docker_prefix,
    )
    expected_ingress_image = compose["services"]["teams-public-ingress"].get("image")
    ingress_image_id = static_preflight.get("ownedStagingIngressImageId")
    if not isinstance(expected_ingress_image, str) or ingress_image_id != _locked_image_ids(
        image_lock
    ).get(expected_ingress_image):
        raise CanaryGateError("preflight did not confirm the locked Stage 1 ingress image owner")

    # ── Phase 2: Compare the existing and proposed host Caddy routes ────
    # 第二阶段：比较现有与候选宿主机 Caddy 配置。
    hostname, source_digest, candidate_digest = _validate_caddy_inputs(
        source_file=source_caddyfile,
        active_file=ACTIVE_CADDYFILE,
        candidate_file=candidate_caddyfile,
        receipt_file=caddy_receipt,
        backend_ca_file=backend_ca_file,
        caddy_binary=caddy_binary,
    )
    _verify_public_ca_file(backend_ca_file, openssl_binary)

    # ── Phase 3: Verify both TLS paths and the real model probe ─────────
    # 第三阶段：验证两段 TLS 和真实模型探测。
    _verify_tls_handshake(
        openssl_binary=openssl_binary,
        connect="127.0.0.1:18443",
        hostname=hostname,
        ca_file=backend_ca_file,
        label="staging backend TLS",
    )
    _verify_tls_handshake(
        openssl_binary=openssl_binary,
        connect=f"{hostname}:443",
        hostname=hostname,
        ca_file=None,
        label="current public TLS",
    )
    _verify_acp_probe(
        plan=acp_plan,
        result=acp_result,
        result_file=acp_result_path,
        script_file=acp_script_path,
        image_lock=image_lock,
        configured_model=(
            compose.get("services", {})
            .get("navigator", {})
            .get("environment", {})
            .get("CYRENE_HARNESS_CODEBUDDY_ACP_MODEL", "")
        ),
        now=datetime.now(UTC),
        maximum_age=timedelta(seconds=maximum_probe_age_seconds),
    )

    return {
        "schemaVersion": "cyrene-host-fronted-canary-readiness-v1",
        "status": CANARY_STATUS,
        "canaryReady": True,
        "deploymentReady": False,
        "accepted": False,
        "stage1StaticPreflight": "PASS",
        "stage1Services": service_states,
        "ingressImageId": ingress_image_id,
        "hostCaddySourceSha256": source_digest,
        "hostCaddyCandidateSha256": candidate_digest,
        "backendTls": "PASS",
        "publicTls": "PASS",
        "codeBuddyAcpProbe": "PASS",
        "fullTeamsAcceptance": "NOT_RUN",
        "cloudProviderAcceptance": "NOT_RUN",
        "twentyFourHourObservation": "NOT_RUN",
        "huggingFace": "DISABLED",
        "staticPreflightStatus": static_preflight.get("status"),
    }


def _arguments() -> argparse.Namespace:
    """Parse the explicit file and limit inputs for the read-only gate."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, action="append", required=True)
    parser.add_argument("--env-file", type=Path, action="append", required=True)
    parser.add_argument("--image-lock", type=Path, required=True)
    parser.add_argument("--secrets-dir", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--source-caddyfile", type=Path, required=True)
    parser.add_argument("--candidate-caddyfile", type=Path, required=True)
    parser.add_argument("--caddy-receipt", type=Path, required=True)
    parser.add_argument("--backend-ca-file", type=Path, required=True)
    parser.add_argument("--acp-plan", type=Path, required=True)
    parser.add_argument("--acp-result", type=Path, required=True)
    parser.add_argument("--acp-script", type=Path, required=True)
    parser.add_argument("--caddy-binary", default="/usr/local/bin/caddy")
    parser.add_argument("--openssl-binary", default="openssl")
    parser.add_argument("--docker-prefix", nargs="+", default=["sudo", "-n", "/usr/bin/docker"])
    parser.add_argument("--max-memory-mib", type=int, default=3584)
    parser.add_argument("--max-cpus", type=float, default=1.95)
    parser.add_argument("--required-free-gib", type=float, default=2.0)
    parser.add_argument("--minimum-mem-available-mib", type=int, default=1024)
    parser.add_argument("--maximum-probe-age-seconds", type=int, default=1800)
    return parser.parse_args()


def main() -> int:
    """Emit a machine-readable readiness result and never change deployment state."""

    arguments = _arguments()
    try:
        result = run_canary_gate(
            compose_files=[path.resolve() for path in arguments.compose_file],
            env_files=[path.resolve() for path in arguments.env_file],
            image_lock_path=arguments.image_lock.resolve(),
            secrets_dir=arguments.secrets_dir.resolve(),
            config_dir=arguments.config_dir.resolve(),
            source_caddyfile=arguments.source_caddyfile.resolve(),
            candidate_caddyfile=arguments.candidate_caddyfile.resolve(),
            caddy_receipt=arguments.caddy_receipt.resolve(),
            backend_ca_file=arguments.backend_ca_file.resolve(),
            acp_plan_path=arguments.acp_plan.resolve(),
            acp_result_path=arguments.acp_result.resolve(),
            acp_script_path=arguments.acp_script.resolve(),
            caddy_binary=arguments.caddy_binary,
            openssl_binary=arguments.openssl_binary,
            docker_prefix=arguments.docker_prefix,
            max_memory_mib=arguments.max_memory_mib,
            max_cpus=arguments.max_cpus,
            required_free_gib=arguments.required_free_gib,
            minimum_mem_available_mib=arguments.minimum_mem_available_mib,
            maximum_probe_age_seconds=arguments.maximum_probe_age_seconds,
        )
    except (
        CanaryGateError,
        preflight.PreflightError,
        BuildError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        result = {
            "schemaVersion": "cyrene-host-fronted-canary-readiness-v1",
            "status": CANARY_BLOCKED_STATUS,
            "canaryReady": False,
            "deploymentReady": False,
            "accepted": False,
            "blockedReason": str(error),
            "fullTeamsAcceptance": "NOT_RUN",
            "cloudProviderAcceptance": "NOT_RUN",
            "twentyFourHourObservation": "NOT_RUN",
            "huggingFace": "DISABLED",
        }
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
