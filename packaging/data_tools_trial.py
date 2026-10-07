#!/usr/bin/env python3
"""Start and manage an isolated Catalyst/Echo Linux trial runtime.

The launcher creates real local DirectPluginRuntime endpoints from the pinned
package manifests, persists only their non-secret connection references, and
keeps the two Product databases separate while sharing one artifact directory.
试用启动器从 Plugin manifest 启动真实本地端点，独立保存 Product 数据库并共享制品目录。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = "cyrene.data-tools.runtime.v1"
HEALTH_PATH = "/healthz"
PLUGIN_SPECS = (
    ("document-parsing", "document.parsing.v1", "CYRENE_DOCUMENT_PARSING_CONNECTION_REF"),
    (
        "knowledge-preparation",
        "dataset.knowledge.v1",
        "CYRENE_KNOWLEDGE_PREPARATION_CONNECTION_REF",
    ),
    ("dataset-generation", "dataset.generation.v1", "CYRENE_DATASET_GENERATION_CONNECTION_REF"),
    ("dataset-preparation", "dataset.preparation.v1", "CYRENE_DATASET_PREPARATION_CONNECTION_REF"),
    ("exact-match", "evaluation.runner.v1", "CYRENE_EVALUATION_RUNNER_CONNECTION_REF"),
)
PRODUCTS = ("catalyst", "echo")
TOKEN_ENV = "CYRENE_DATA_TOOLS_TOKEN"
CAPABILITY_BINDING_ENV = "CYRENE_CAPABILITY_BINDING_ID"
CAPABILITY_CONFIGURATION_ENV = "CYRENE_CAPABILITY_CONFIGURATION_JSON"
DEFAULT_GENERATION_BINDING_ID = "data-tools-trial-model"
MAX_GENERATION_CONFIGURATION_BYTES = 64 * 1024
TOKEN_MIN_LENGTH = 32
STARTUP_TIMEOUT_SECONDS = 90.0
STOP_TIMEOUT_SECONDS = 8.0


class TrialLauncherError(RuntimeError):
    """Raised when the local trial cannot be started or safely managed."""


@dataclass
class ProcessHandle:
    """One supervised process and its optional readiness-event file."""

    name: str
    process: subprocess.Popen[str]
    record: dict[str, Any]
    ready_path: Path | None = None


def _default_state_dir() -> Path:
    """Return the per-user persistent state location."""

    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_root / "cyrene" / "data-tools-trial"


def _utc_now() -> str:
    """Return a sortable UTC timestamp with an explicit timezone."""

    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _json_bytes(value: Any) -> bytes:
    """Encode stable UTF-8 JSON for atomic runtime state writes."""

    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    """Write a JSON document by replacing a same-directory temporary file."""

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_state_dir(value: Path, *, create: bool) -> Path:
    """Resolve a real state directory and refuse a symlink at its leaf."""

    path = value.expanduser().absolute()
    if path.is_symlink():
        raise TrialLauncherError(f"state directory must not be a symlink: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists() or not path.is_dir():
        raise TrialLauncherError(f"state directory does not exist: {path}")
    return path.resolve()


def _runtime_path(state_dir: Path) -> Path:
    """Return the canonical local runtime record path."""

    return state_dir / "runtime.json"


def _load_runtime(state_dir: Path) -> dict[str, Any]:
    """Load runtime metadata and validate its identifying fields."""

    path = _runtime_path(state_dir)
    if path.is_symlink() or not path.is_file():
        raise TrialLauncherError(f"runtime.json is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TrialLauncherError(f"cannot read runtime.json: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != SCHEMA_VERSION
        or value.get("stateDir") != str(state_dir)
        or not isinstance(value.get("processes"), list)
    ):
        raise TrialLauncherError("runtime.json does not identify this data-tools trial")
    return value


def _acquire_lock(state_dir: Path):
    """Lock lifecycle operations to avoid two launchers racing on one instance."""

    lock_path = state_dir / ".launcher.lock"
    if lock_path.is_symlink():
        raise TrialLauncherError(f"launcher lock must not be a symlink: {lock_path}")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    stream = os.fdopen(descriptor, "r+")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        stream.close()
        raise TrialLauncherError(
            "another launcher operation is using this state directory"
        ) from error
    return stream


def _acquire_environment_lock(venv: Path):
    """Serialize installers that share a prewarmed task-root virtualenv."""

    lock_path = venv / ".data-tools-trial.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    stream = os.fdopen(descriptor, "r+")
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    return stream


def _redact(text: str, token: str | None) -> str:
    """Remove the bearer value before child output is written or displayed."""

    return text.replace(token, "[redacted]") if token else text


def _proc_start_ticks(pid: int) -> int | None:
    """Read the Linux process start counter used to detect reused PIDs."""

    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return int(fields[21])
    except (OSError, ValueError, IndexError):
        return None


def _proc_cmdline(pid: int) -> list[str] | None:
    """Read the process argument vector without exposing its environment."""

    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _process_matches(record: dict[str, Any]) -> bool:
    """Require the saved PID, start counter, and executable to match."""

    try:
        pid = int(record["pid"])
        expected_ticks = int(record["startTicks"])
        expected_executable = str(record["executable"])
    except (KeyError, TypeError, ValueError):
        return False
    command = _proc_cmdline(pid)
    expected_argv = record.get("processArgv", record.get("argv"))
    process_executable = str(record.get("processExecutable", expected_executable))
    if isinstance(expected_argv, list) and all(isinstance(part, str) for part in expected_argv):
        command_matches = command is not None and (
            command[: len(expected_argv)] == expected_argv or command[1:] == expected_argv
        )
    else:
        # Executable console scripts appear as [python, script, args...] in
        # /proc/cmdline even when Popen received [script, args...].
        command_matches = command is not None and (
            (bool(command) and command[0] == process_executable)
            or (len(command) > 1 and command[1] == process_executable)
        )
    return command_matches and _proc_start_ticks(pid) == expected_ticks


def _runtime_env(token: str | None) -> dict[str, str]:
    """Copy the caller environment while keeping credentials out of argv/state."""

    environment = os.environ.copy()
    environment.pop(CAPABILITY_BINDING_ENV, None)
    environment.pop(CAPABILITY_CONFIGURATION_ENV, None)
    if token:
        environment[TOKEN_ENV] = token
    return environment


def _is_loopback(host: str) -> bool:
    """Return whether a listener address is loopback-only."""

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_bind(args: argparse.Namespace) -> tuple[str | None, bool]:
    """Enforce explicit auth for remote listeners without printing token data."""

    host = str(args.host)
    try:
        if not 1 <= int(args.catalyst_port) <= 65535:
            raise TrialLauncherError("--catalyst-port must be between 1 and 65535")
        if not 1 <= int(args.echo_port) <= 65535:
            raise TrialLauncherError("--echo-port must be between 1 and 65535")
        if int(args.catalyst_port) == int(args.echo_port):
            raise TrialLauncherError("Catalyst and Echo must use distinct API ports")
    except ValueError as error:
        raise TrialLauncherError("API ports must be integers") from error
    remote = not _is_loopback(host)
    token = os.environ.get(TOKEN_ENV)
    if remote and not args.allow_remote:
        raise TrialLauncherError("non-loopback listeners require --allow-remote")
    if remote and (
        token is None
        or len(token) < TOKEN_MIN_LENGTH
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in token)
    ):
        raise TrialLauncherError(
            f"remote listeners require {TOKEN_ENV} with at least {TOKEN_MIN_LENGTH} printable ASCII bytes"
        )
    return token, remote


def _project_root(source_root: Path, name: str) -> Path:
    """Resolve one sibling Product or Plugins repository in the task source tree."""

    path = source_root / name
    if path.is_symlink() or not path.is_dir():
        raise TrialLauncherError(f"required {name} source directory is missing: {path}")
    return path.resolve()


def _plugin_package_path(plugins_root: Path, plugin_name: str) -> Path:
    """Map the five required stateless capabilities to package directories."""

    return (
        plugins_root
        / "plugins"
        / {
            "document-parsing": "tools/document-parsing",
            "knowledge-preparation": "tools/knowledge-preparation",
            "dataset-generation": "tools/dataset-generation",
            "dataset-preparation": "tools/dataset-preparation",
            "exact-match": "evaluation/exact-match",
        }[plugin_name]
    )


def _plugin_manifest(package_root: Path, expected_capability: str) -> dict[str, str]:
    """Read manifest identity and the real module entrypoint for one Plugin."""

    manifest_path = package_root / "plugin.manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise TrialLauncherError(f"Plugin manifest is missing or unsafe: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TrialLauncherError(f"cannot read Plugin manifest {manifest_path}: {error}") from error
    runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
    methods = manifest.get("methods") if isinstance(manifest, dict) else None
    capabilities = manifest.get("capabilities") if isinstance(manifest, dict) else None
    entrypoint = runtime.get("entrypoint") if isinstance(runtime, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("kind") != "capability-plugin"
        or capabilities != [expected_capability]
        or not isinstance(entrypoint, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", entrypoint)
        or not isinstance(methods, list)
        or not methods
    ):
        raise TrialLauncherError(
            f"Plugin manifest does not declare the expected capability: {manifest_path}"
        )
    versions = {
        method.get("interfaceVersion")
        for method in methods
        if isinstance(method, dict) and isinstance(method.get("interfaceVersion"), str)
    }
    if len(versions) != 1 or any(not isinstance(method, dict) for method in methods):
        raise TrialLauncherError(
            f"Plugin methods must share one interface version: {manifest_path}"
        )
    return {
        "pluginId": str(manifest.get("id", "")),
        "capability": expected_capability,
        "entrypoint": entrypoint,
        "interfaceVersion": next(iter(versions)),
    }


def _validate_provider_connection_ref(value: Any) -> str:
    """Require a resolved local gRPC or Unix ref; reject HTTP and credentials."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise TrialLauncherError("generation config model_endpoint must be a local connection ref")
    reference = urlsplit(value)
    if reference.scheme == "grpc":
        try:
            port = reference.port
        except ValueError:
            port = None
        valid = (
            reference.hostname in {"127.0.0.1", "localhost", "::1"}
            and port is not None
            and 1 <= port <= 65535
            and reference.username is None
            and reference.password is None
            and reference.path in {"", "/"}
            and not reference.query
            and not reference.fragment
        )
    elif reference.scheme == "unix":
        valid = (
            not reference.netloc
            and reference.path.startswith("/")
            and not reference.query
            and not reference.fragment
        )
    else:
        valid = False
    if not valid:
        raise TrialLauncherError(
            "generation config model_endpoint must be a loopback gRPC or absolute Unix connection ref"
        )
    return value


def _read_generation_config(path: Path) -> dict[str, Any]:
    """Read the public dataset-generation config without retaining it on disk."""

    source = path.expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise TrialLauncherError(f"generation config must be a regular file: {source}")
    if source.stat().st_size > MAX_GENERATION_CONFIGURATION_BYTES:
        raise TrialLauncherError(
            f"generation config exceeds {MAX_GENERATION_CONFIGURATION_BYTES} bytes"
        )
    try:
        config = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TrialLauncherError(f"cannot read generation config: {error}") from error
    allowed = {
        "model_endpoint",
        "model",
        "temperature",
        "max_tokens_per_call",
        "timeout_seconds",
    }
    if not isinstance(config, dict) or set(config) - allowed:
        raise TrialLauncherError(
            "generation config must be an object containing only model_endpoint, model, temperature, max_tokens_per_call, and timeout_seconds"
        )
    if "model_endpoint" not in config or "model" not in config:
        raise TrialLauncherError("generation config requires model_endpoint and model")
    config["model_endpoint"] = _validate_provider_connection_ref(config["model_endpoint"])
    if not isinstance(config["model"], str) or not config["model"].strip():
        raise TrialLauncherError("generation config model must be a non-empty string")
    if "temperature" in config and (
        isinstance(config["temperature"], bool)
        or not isinstance(config["temperature"], (int, float))
        or not math.isfinite(config["temperature"])
        or not 0 <= config["temperature"] <= 2
    ):
        raise TrialLauncherError("generation config temperature must be between 0 and 2")
    if "max_tokens_per_call" in config and (
        isinstance(config["max_tokens_per_call"], bool)
        or not isinstance(config["max_tokens_per_call"], int)
        or not 1 <= config["max_tokens_per_call"] <= 8192
    ):
        raise TrialLauncherError("generation config max_tokens_per_call must be between 1 and 8192")
    if "timeout_seconds" in config and (
        isinstance(config["timeout_seconds"], bool)
        or not isinstance(config["timeout_seconds"], (int, float))
        or not math.isfinite(config["timeout_seconds"])
        or not 0 < config["timeout_seconds"] <= 300
    ):
        raise TrialLauncherError(
            "generation config timeout_seconds must be above 0 and at most 300"
        )
    return config


def _validate_generation_binding_id(value: str) -> str:
    """Validate the non-secret activation binding identifier."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise TrialLauncherError("--generation-binding-id must be a non-empty safe identifier")
    return value


def _verify_model_provider_endpoint(
    connection_ref: str,
    *,
    plugin_python: Path,
    environment: dict[str, str],
) -> None:
    """Health-check the configured local provider without making a model call."""

    probe = """
import os
import sys
import grpc
from cyrene_plugin_runtime._generated import direct_plugin_runtime_pb2 as wire
from cyrene_plugin_runtime._generated import direct_plugin_runtime_pb2_grpc as wire_grpc

reference = os.environ["CYRENE_TRIAL_MODEL_PROVIDER_REF"]
target = reference.removeprefix("grpc://")
channel = grpc.insecure_channel(target)
try:
    response = wire_grpc.DirectPluginRuntimeStub(channel).Health(wire.HealthRequest(), timeout=4)
finally:
    channel.close()
if response.status != wire.HealthResponse.STATUS_SERVING:
    sys.exit(2)
if "model.provider.v1" not in response.capabilities:
    sys.exit(3)
"""
    probe_environment = environment.copy()
    probe_environment.pop(TOKEN_ENV, None)
    probe_environment["CYRENE_TRIAL_MODEL_PROVIDER_REF"] = connection_ref
    try:
        result = subprocess.run(
            [str(plugin_python), "-s", "-c", probe],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
            env=probe_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TrialLauncherError(
            "configured model.provider.v1 endpoint did not answer its local health check"
        ) from error
    if result.returncode != 0:
        raise TrialLauncherError(
            "configured model.provider.v1 endpoint must be serving the expected capability"
        )


def _run_checked(
    command: list[str], *, environment: dict[str, str], cwd: Path, token: str | None
) -> None:
    """Run a setup command and report bounded, credential-redacted diagnostics."""

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise TrialLauncherError(f"could not run {Path(command[0]).name}: {error}") from error
    if result.returncode != 0:
        detail = _redact((result.stderr or result.stdout or "").strip(), token)
        if len(detail) > 6000:
            detail = detail[-6000:]
        raise TrialLauncherError(
            f"{Path(command[0]).name} setup failed with exit {result.returncode}:\n{detail}"
        )


def _source_fingerprint(paths: tuple[Path, ...]) -> str:
    """Hash package declarations so changed dependency metadata triggers setup."""

    digest = hashlib.sha256()
    for root in paths:
        for relative in ("pyproject.toml", "plugin.manifest.json"):
            file_path = root / relative
            if file_path.is_file():
                digest.update(str(file_path.resolve()).encode("utf-8"))
                digest.update(file_path.read_bytes())
    return digest.hexdigest()


def _prepare_plugin_environment(
    *,
    plugins_root: Path,
    state_dir: Path,
    token: str | None,
    environment: dict[str, str],
    preferred_environment: Path | None,
) -> Path:
    """Install the manifest-owned Plugins into a per-trial virtual environment."""

    uv = shutil.which("uv")
    if uv is None:
        raise TrialLauncherError("uv is required to install the local trial runtime")
    package_roots = tuple(
        _plugin_package_path(plugins_root, package_name) for package_name, _, _ in PLUGIN_SPECS
    )
    for package_root in package_roots:
        if package_root.is_symlink() or not (package_root / "pyproject.toml").is_file():
            raise TrialLauncherError(f"Plugin package source is missing: {package_root}")
    sdk_roots = (
        plugins_root / "sdk/python/cyrene_plugin_runtime",
        plugins_root / "sdk/python/cyrene_model_provider_contracts",
    )
    install_roots = (*sdk_roots, *package_roots)
    use_preferred = (
        preferred_environment is not None and (preferred_environment / "bin/python").is_file()
    )
    venv = preferred_environment if use_preferred else state_dir / "venvs" / "plugins"
    venv_python = venv / "bin/python"
    marker = state_dir / "venvs" / ".plugins-source.json"
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fingerprint = _source_fingerprint(install_roots)
    marker_matches = False
    if marker.is_file() and not marker.is_symlink():
        try:
            marker_matches = (
                json.loads(marker.read_text(encoding="utf-8")).get("fingerprint") == fingerprint
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            marker_matches = False
    if not use_preferred and not venv_python.is_file():
        _run_checked(
            [uv, "venv", "--python", sys.executable, str(venv)],
            environment=environment,
            cwd=plugins_root,
            token=token,
        )
    if not marker_matches:
        command = [uv, "pip", "install", "--python", str(venv_python)]
        if use_preferred:
            command.append("--no-deps")
        for install_root in install_roots:
            command.extend(("--editable", str(install_root)))
        _run_checked(command, environment=environment, cwd=plugins_root, token=token)
        _atomic_json(marker, {"fingerprint": fingerprint, "updatedAt": _utc_now()})
    return venv_python


def _prepare_product_environment(
    *,
    product: str,
    product_root: Path,
    state_dir: Path,
    token: str | None,
    environment: dict[str, str],
    preferred_environment: Path | None,
) -> Path:
    """Sync locked Product dependencies into trial state and install editable source."""

    uv = shutil.which("uv")
    if uv is None:
        raise TrialLauncherError("uv is required to install the local trial runtime")
    use_preferred = (
        preferred_environment is not None and (preferred_environment / "bin/python").is_file()
    )
    venv = preferred_environment if use_preferred else state_dir / "venvs" / product
    product_environment = environment.copy()
    product_environment.pop(TOKEN_ENV, None)
    product_environment["UV_PROJECT_ENVIRONMENT"] = str(venv)
    if not use_preferred:
        _run_checked(
            [
                uv,
                "sync",
                "--project",
                str(product_root),
                "--python",
                sys.executable,
                "--frozen",
                "--no-dev",
                "--no-install-project",
            ],
            environment=product_environment,
            cwd=product_root,
            token=token,
        )
    venv_python = venv / "bin/python"
    _run_checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(venv_python),
            "--no-deps",
            "--editable",
            str(product_root),
        ],
        environment=product_environment,
        cwd=product_root,
        token=token,
    )
    return venv


def _prepare_web_host_environment(
    *,
    navigator_root: Path,
    state_dir: Path,
    token: str | None,
    environment: dict[str, str],
    preferred_environment: Path | None,
) -> Path:
    """Install Navigator's locked Web Host dependencies into a usable Python env."""

    uv = shutil.which("uv")
    if uv is None:
        raise TrialLauncherError("uv is required to install the Navigator Web Host")
    use_preferred = (
        preferred_environment is not None and (preferred_environment / "bin/python").is_file()
    )
    venv = preferred_environment if use_preferred else state_dir / "venvs" / "navigator"
    web_environment = environment.copy()
    web_environment.pop(TOKEN_ENV, None)
    web_environment["UV_PROJECT_ENVIRONMENT"] = str(venv)
    if not use_preferred:
        _run_checked(
            [
                uv,
                "sync",
                "--project",
                str(navigator_root),
                "--python",
                sys.executable,
                "--frozen",
                "--no-dev",
                "--no-install-project",
            ],
            environment=web_environment,
            cwd=navigator_root,
            token=token,
        )
    _run_checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(venv / "bin/python"),
            "--no-deps",
            "--editable",
            str(navigator_root),
        ],
        environment=web_environment,
        cwd=navigator_root,
        token=token,
    )
    return venv


def _launch_process(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    log_path: Path,
    capture_plugin_ready: bool = False,
) -> ProcessHandle:
    """Start a detached supervisor that keeps child logs redacted after startup."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path = (
        log_path.with_suffix(log_path.suffix + ".ready.json") if capture_plugin_ready else None
    )
    if ready_path is not None:
        if ready_path.is_symlink():
            raise TrialLauncherError(f"refusing an unsafe Plugin readiness path: {ready_path}")
        ready_path.unlink(missing_ok=True)
    supervisor_path = Path(__file__).with_name("data_tools_process.py")
    supervisor_command = [
        sys.executable,
        "-s",
        str(supervisor_path),
        "--log-path",
        str(log_path),
    ]
    if ready_path is not None:
        supervisor_command.extend(("--ready-event-path", str(ready_path)))
    supervisor_command.extend(("--", *command))
    try:
        process = subprocess.Popen(
            supervisor_command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise TrialLauncherError(f"could not start {name}: {error}") from error
    record = {
        "name": name,
        "pid": process.pid,
        "processGroup": process.pid,
        "startTicks": _proc_start_ticks(process.pid),
        "executable": command[0],
        "argv": command,
        "processExecutable": supervisor_command[0],
        "processArgv": supervisor_command,
        "logPath": str(log_path),
    }
    if record["startTicks"] is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        raise TrialLauncherError("Linux /proc process identity is unavailable")
    return ProcessHandle(name=name, process=process, record=record, ready_path=ready_path)


def _plugin_record(
    *,
    handle: ProcessHandle,
    expected: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """Require the Plugin runtime's actual direct endpoint readiness record."""

    if handle.ready_path is None:
        raise TrialLauncherError("Plugin readiness capture was not enabled for this process")
    deadline = time.monotonic() + timeout
    value: Any = None
    while time.monotonic() < deadline:
        if handle.process.poll() is not None:
            raise TrialLauncherError(
                f"Plugin {expected['pluginId']} exited before readiness; see {handle.record['logPath']}"
            )
        try:
            if handle.ready_path.is_symlink():
                raise TrialLauncherError("Plugin readiness event path became a symlink")
            value = json.loads(handle.ready_path.read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            time.sleep(0.05)
        except json.JSONDecodeError:
            time.sleep(0.05)
    else:
        raise TrialLauncherError(
            f"Plugin {expected['pluginId']} did not report a direct endpoint; see {handle.record['logPath']}"
        )
    interface_versions = value.get("interface_versions") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("event") != "direct_plugin_ready"
        or value.get("capability") != expected["capability"]
        or not isinstance(interface_versions, list)
        or expected["interfaceVersion"] not in interface_versions
        or not isinstance(value.get("connection_ref"), str)
    ):
        raise TrialLauncherError(
            f"Plugin {expected['pluginId']} did not return the expected capability endpoint"
        )
    if handle.process.poll() is not None:
        raise TrialLauncherError(
            f"Plugin {expected['pluginId']} exited after readiness; see {handle.record['logPath']}"
        )
    handle.ready_path.unlink(missing_ok=True)
    reference = urlsplit(value["connection_ref"])
    try:
        port = reference.port
    except ValueError:
        port = None
    if (
        reference.scheme != "grpc"
        or reference.hostname not in {"127.0.0.1", "localhost", "::1"}
        or reference.username is not None
        or reference.password is not None
        or reference.path not in {"", "/"}
        or reference.query
        or reference.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        raise TrialLauncherError(
            f"Plugin {expected['pluginId']} returned an invalid loopback gRPC connection reference"
        )
    return {
        **expected,
        "connection_ref": value["connection_ref"],
        "endpointEvent": value,
        "pid": handle.process.pid,
        "logPath": handle.record["logPath"],
    }


def _health_check(url: str, *, timeout: float = 1.5) -> bool:
    """Return true only when a Product's own health route responds successfully."""

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _wait_health(handle: ProcessHandle, url: str, timeout: float) -> None:
    """Wait for a Product health response and fail if its process exits early."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if handle.process.poll() is not None:
            raise TrialLauncherError(
                f"{handle.name} exited with status {handle.process.returncode}; see {handle.record['logPath']}"
            )
        if _health_check(url):
            return
        time.sleep(0.25)
    raise TrialLauncherError(f"{handle.name} did not become healthy at {url}")


def _terminate_handles(handles: list[ProcessHandle]) -> None:
    """Stop newly started process groups in reverse dependency order."""

    for handle in reversed(handles):
        if handle.process.poll() is None:
            try:
                os.killpg(handle.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    for handle in reversed(handles):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            handle.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(handle.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            handle.process.wait(timeout=2)


def _write_runtime(state_dir: Path, value: dict[str, Any]) -> None:
    """Persist the secret-free runtime record with owner-only permissions."""

    value["updatedAt"] = _utc_now()
    _atomic_json(_runtime_path(state_dir), value)


def _start(args: argparse.Namespace) -> int:
    """Prepare dependencies, start the Plugins, then both Product APIs."""

    state_dir = _safe_state_dir(args.state_dir, create=True)
    lock = _acquire_lock(state_dir)
    shared_environment_lock = None
    handles: list[ProcessHandle] = []
    pairing_code_path: Path | None = None
    try:
        if _runtime_path(state_dir).exists():
            existing = _load_runtime(state_dir)
            if existing.get("status") == "running" and any(
                _process_matches(record) for record in existing["processes"]
            ):
                raise TrialLauncherError("this data-tools trial is already running; use stop first")
        token, remote = _validate_bind(args)
        generation_config = (
            _read_generation_config(args.generation_config)
            if args.generation_config is not None
            else None
        )
        if generation_config is not None:
            generation_binding_id = _validate_generation_binding_id(
                args.generation_binding_id or DEFAULT_GENERATION_BINDING_ID
            )
        elif args.generation_binding_id is not None:
            raise TrialLauncherError("--generation-binding-id requires --generation-config")
        else:
            generation_binding_id = None
        source_root = args.source_root.expanduser().absolute().resolve()
        catalyst_root = _project_root(source_root, "catalyst")
        echo_root = _project_root(source_root, "echo")
        plugins_root = _project_root(source_root, "plugins")
        for package_name, capability, _ in PLUGIN_SPECS:
            _plugin_manifest(_plugin_package_path(plugins_root, package_name), capability)

        ui_ports: dict[str, int] = {}
        if args.with_ui:
            for name in ("navigator", "client", "control"):
                port = int(getattr(args, f"{name}_port"))
                if not 1 <= port <= 65535:
                    raise TrialLauncherError(f"--{name}-port must be between 1 and 65535")
                if port in {args.catalyst_port, args.echo_port} or port in ui_ports.values():
                    raise TrialLauncherError(
                        "API, Navigator, Client, and Control ports must be distinct"
                    )
                ui_ports[name] = port
            if remote:
                raise TrialLauncherError(
                    "--with-ui is a loopback development surface; expose only the APIs through your TLS terminator"
                )
        if args.startup_timeout <= 0:
            raise TrialLauncherError("--startup-timeout must be positive")
        if args.plugin_port_base:
            if not 1 <= args.plugin_port_base <= 65531:
                raise TrialLauncherError(
                    "--plugin-port-base must be 1 through 65531 so all five local endpoints fit"
                )
            plugin_ports = set(
                range(args.plugin_port_base, args.plugin_port_base + len(PLUGIN_SPECS))
            )
            reserved_ports = {args.catalyst_port, args.echo_port, *ui_ports.values()}
            if plugin_ports & reserved_ports:
                raise TrialLauncherError("Plugin endpoint ports must not overlap API or UI ports")

        environment = _runtime_env(token)
        plugin_setup_environment = environment.copy()
        plugin_setup_environment.pop(TOKEN_ENV, None)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifacts = state_dir / "artifacts"
        catalyst_home = state_dir / "catalyst"
        echo_home = state_dir / "echo"
        logs = state_dir / "logs"
        for directory in (artifacts, catalyst_home, echo_home, logs):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

        preferred_environment = source_root / ".venv"
        if (preferred_environment / "bin/python").is_file():
            shared_environment_lock = _acquire_environment_lock(preferred_environment)
        plugin_python = _prepare_plugin_environment(
            plugins_root=plugins_root,
            state_dir=state_dir,
            token=token,
            environment=plugin_setup_environment,
            preferred_environment=preferred_environment,
        )
        if generation_config is not None:
            _verify_model_provider_endpoint(
                generation_config["model_endpoint"],
                plugin_python=plugin_python,
                environment=plugin_setup_environment,
            )
        plugin_env = plugin_setup_environment.copy()
        plugin_endpoints: dict[str, str] = {}
        plugin_records: list[dict[str, Any]] = []
        process_records: list[dict[str, Any]] = []
        for index, (package_name, capability, variable) in enumerate(PLUGIN_SPECS):
            package_root = _plugin_package_path(plugins_root, package_name)
            expected = _plugin_manifest(package_root, capability)
            port = args.plugin_port_base + index if args.plugin_port_base else 0
            listen = f"127.0.0.1:{port}"
            command = [
                str(plugin_python),
                "-s",
                "-m",
                "cyrene_plugin_runtime.server",
                "--entrypoint",
                expected["entrypoint"],
                "--capability",
                capability,
                "--interface-version",
                expected["interfaceVersion"],
                "--listen",
                listen,
            ]
            process_environment = plugin_env.copy()
            if package_name == "dataset-generation" and generation_config is not None:
                process_environment[CAPABILITY_BINDING_ENV] = generation_binding_id or ""
                process_environment[CAPABILITY_CONFIGURATION_ENV] = json.dumps(
                    generation_config, separators=(",", ":"), sort_keys=True
                )
            handle = _launch_process(
                name=f"plugin-{package_name}",
                command=command,
                cwd=plugins_root,
                environment=process_environment,
                log_path=logs / f"plugin-{package_name}.log",
                capture_plugin_ready=True,
            )
            handles.append(handle)
            record = _plugin_record(handle=handle, expected=expected, timeout=args.startup_timeout)
            plugin_records.append(record)
            process_records.append(handle.record)
            plugin_endpoints[variable] = record["connection_ref"]

        catalyst_env = environment.copy()
        catalyst_env.update(plugin_endpoints)
        catalyst_venv = _prepare_product_environment(
            product="catalyst",
            product_root=catalyst_root,
            state_dir=state_dir,
            token=token,
            environment=catalyst_env,
            preferred_environment=preferred_environment,
        )
        echo_env = environment.copy()
        echo_env["CYRENE_EVALUATION_RUNNER_CONNECTION_REF"] = plugin_endpoints[
            "CYRENE_EVALUATION_RUNNER_CONNECTION_REF"
        ]
        echo_venv = _prepare_product_environment(
            product="echo",
            product_root=echo_root,
            state_dir=state_dir,
            token=token,
            environment=echo_env,
            preferred_environment=preferred_environment,
        )

        host = args.host
        local_host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
        catalyst_url_host = (
            f"[{local_host}]"
            if ":" in local_host and not local_host.startswith("[")
            else local_host
        )
        catalyst_url = f"http://{catalyst_url_host}:{args.catalyst_port}"
        catalyst_command = [
            str(catalyst_venv / "bin/cyrene-catalyst"),
            "serve",
            "--home",
            str(catalyst_home),
            "--artifact-root",
            str(artifacts),
            "--host",
            host,
            "--port",
            str(args.catalyst_port),
        ]
        echo_command = [
            str(echo_venv / "bin/cyrene-echo"),
            "serve",
            "--database",
            str(echo_home / "echo.sqlite3"),
            "--artifact-root",
            str(artifacts),
            "--catalyst-url",
            catalyst_url,
            "--host",
            host,
            "--port",
            str(args.echo_port),
        ]
        if remote:
            catalyst_command.append("--allow-remote")
            echo_command.append("--allow-remote")

        catalyst_handle = _launch_process(
            name="catalyst",
            command=catalyst_command,
            cwd=catalyst_root,
            environment=catalyst_env,
            log_path=logs / "catalyst.log",
        )
        handles.append(catalyst_handle)
        process_records.append(catalyst_handle.record)
        echo_handle = _launch_process(
            name="echo",
            command=echo_command,
            cwd=echo_root,
            environment=echo_env,
            log_path=logs / "echo.log",
        )
        handles.append(echo_handle)
        process_records.append(echo_handle.record)

        catalyst_base = catalyst_url
        echo_url_host = (
            f"[{local_host}]"
            if ":" in local_host and not local_host.startswith("[")
            else local_host
        )
        echo_base = f"http://{echo_url_host}:{args.echo_port}"
        _wait_health(catalyst_handle, catalyst_base + HEALTH_PATH, args.startup_timeout)
        _wait_health(echo_handle, echo_base + HEALTH_PATH, args.startup_timeout)

        ui_runtime: dict[str, Any] | None = None
        pairing_code: str | None = None
        if args.with_ui:
            client_root = source_root / "client"
            navigator_root = (
                args.navigator_root.expanduser().absolute().resolve()
                if args.navigator_root is not None
                else source_root.parent.parent / "Cyrene-Services" / "Cyrene-Navigator"
            )
            if (
                not (client_root / "scripts/dev.mjs").is_file()
                or not (client_root / "node_modules/vite/bin/vite.js").is_file()
                or not (client_root / "node_modules/tsx/dist/loader.mjs").is_file()
            ):
                raise TrialLauncherError(
                    f"Client source and installed Node dependencies are required for --with-ui: {client_root}"
                )
            if not (navigator_root / "src/cyrene_navigator/web_host.py").is_file():
                raise TrialLauncherError(
                    f"Navigator Web Host source is missing: {navigator_root / 'src/cyrene_navigator/web_host.py'}"
                )
            node = shutil.which("node")
            if node is None:
                raise TrialLauncherError("Node.js 24 or newer is required for --with-ui")
            version = subprocess.run(
                [node, "--version"],
                capture_output=True,
                text=True,
                check=False,
                env={key: value for key, value in os.environ.items() if key != TOKEN_ENV},
            )
            match = re.match(r"v(\d+)", version.stdout.strip())
            if version.returncode != 0 or match is None or int(match.group(1)) < 24:
                raise TrialLauncherError("Node.js 24 or newer is required for --with-ui")
            navigator_venv = _prepare_web_host_environment(
                navigator_root=navigator_root,
                state_dir=state_dir,
                token=token,
                environment=environment,
                preferred_environment=preferred_environment,
            )
            pairing_code = secrets.token_urlsafe(18)
            pairing_code_path = state_dir / "client" / "navigator-pair-code.txt"
            pairing_code_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if pairing_code_path.is_symlink():
                raise TrialLauncherError("refusing an unsafe Navigator pairing-code path")
            pairing_code_path.unlink(missing_ok=True)
            descriptor = os.open(pairing_code_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(pairing_code + "\n")
            ui_env = environment.copy()
            web_host_command = [
                str(navigator_venv / "bin/python"),
                "-s",
                str(Path(__file__).with_name("data_tools_web_host.py")),
                "--navigator-root",
                str(navigator_root),
                "--pairing-code-file",
                str(pairing_code_path),
                "--catalyst-url",
                catalyst_base,
                "--echo-url",
                echo_base,
                "--host",
                "127.0.0.1",
                "--port",
                str(ui_ports["navigator"]),
            ]
            web_host_handle = _launch_process(
                name="navigator-web-host",
                command=web_host_command,
                cwd=navigator_root,
                environment=ui_env,
                log_path=logs / "navigator-web-host.log",
            )
            handles.append(web_host_handle)
            process_records.append(web_host_handle.record)
            web_host_base = f"http://127.0.0.1:{ui_ports['navigator']}"
            _wait_health(
                web_host_handle,
                web_host_base + "/api/v1/system/status",
                args.startup_timeout,
            )

            client_env = environment.copy()
            client_env.pop(TOKEN_ENV, None)
            client_env.update(
                {
                    "STUDIO_NAVIGATOR_URL": web_host_base,
                    "STUDIO_CONTROL_PORT": str(ui_ports["control"]),
                    "STUDIO_PUBLIC_ORIGINS": f"http://127.0.0.1:{ui_ports['client']}",
                    "STUDIO_CONTROL_DATA_DIR": str(state_dir / "client-control"),
                    "STUDIO_MODE": "local",
                }
            )
            client_command = [
                node,
                "scripts/dev.mjs",
                "--port",
                str(ui_ports["client"]),
            ]
            client_handle = _launch_process(
                name="client-control-vite",
                command=client_command,
                cwd=client_root,
                environment=client_env,
                log_path=logs / "client-control-vite.log",
            )
            handles.append(client_handle)
            process_records.append(client_handle.record)
            client_base = f"http://127.0.0.1:{ui_ports['client']}"
            _wait_health(client_handle, client_base + "/", args.startup_timeout)
            ui_runtime = {
                "clientUrl": client_base,
                "navigatorUrl": web_host_base,
                "controlUrl": f"http://127.0.0.1:{ui_ports['control']}",
                "healthUrl": web_host_base + "/api/v1/system/status",
                "ports": ui_ports,
                "navigatorPid": web_host_handle.process.pid,
                "clientPid": client_handle.process.pid,
            }

        runtime = {
            "schemaVersion": SCHEMA_VERSION,
            "instanceId": str(uuid.uuid4()),
            "status": "running",
            "startedAt": _utc_now(),
            "updatedAt": _utc_now(),
            "sourceRoot": str(source_root),
            "stateDir": str(state_dir),
            "bind": {
                "host": host,
                "remote": remote,
                "externalTlsTerminatorRequired": remote,
                "tokenEnvironmentVariable": TOKEN_ENV if remote else None,
            },
            "storage": {
                "artifactRoot": str(artifacts),
                "catalystDatabase": str(catalyst_home / "catalyst.sqlite3"),
                "echoDatabase": str(echo_home / "echo.sqlite3"),
            },
            "services": {
                "catalyst": {
                    "baseUrl": catalyst_base,
                    "healthUrl": catalyst_base + HEALTH_PATH,
                    "port": args.catalyst_port,
                    "pid": catalyst_handle.process.pid,
                },
                "echo": {
                    "baseUrl": echo_base,
                    "healthUrl": echo_base + HEALTH_PATH,
                    "port": args.echo_port,
                    "pid": echo_handle.process.pid,
                },
            },
            "plugins": plugin_records,
            "generation": {
                "qaProviderConfigured": generation_config is not None,
                "bindingId": generation_binding_id,
            },
            "processes": process_records,
        }
        if ui_runtime is not None:
            runtime["ui"] = ui_runtime
        _write_runtime(state_dir, runtime)
        print(f"Catalyst API: {catalyst_base} (health {HEALTH_PATH})")
        print(f"Echo API: {echo_base} (health {HEALTH_PATH})")
        print(f"Runtime record: {_runtime_path(state_dir)}")
        print(f"Shared artifacts: {artifacts}")
        if generation_config is None:
            print(
                "QA generation unavailable: no model.provider.v1 binding configured; "
                "manual SFT preparation remains available."
            )
        else:
            print(
                f"QA generation provider binding configured ({generation_binding_id}); "
                "no model call was made during startup."
            )
        if ui_runtime is not None:
            print(f"Client UI: {ui_runtime['clientUrl']}")
            print(f"Navigator Web Host: {ui_runtime['navigatorUrl']}")
            print(f"One-time Navigator pairing code: {pairing_code}")
        print("Stop without deleting data: python3 packaging/data_tools_trial.py stop")
        if shared_environment_lock is not None:
            shared_environment_lock.close()
            shared_environment_lock = None
        return 0
    except BaseException:
        _terminate_handles(handles)
        if pairing_code_path is not None:
            pairing_code_path.unlink(missing_ok=True)
        raise
    finally:
        if shared_environment_lock is not None:
            shared_environment_lock.close()
        lock.close()


def _status(args: argparse.Namespace) -> int:
    """Report process identity and Product health without secrets."""

    state_dir = _safe_state_dir(args.state_dir, create=False)
    runtime = _load_runtime(state_dir)
    processes = runtime["processes"]
    running = [_process_matches(record) for record in processes]
    services = runtime.get("services", {})
    health = {
        name: _health_check(str(service.get("healthUrl", "")))
        for name, service in services.items()
        if isinstance(service, dict)
    }
    ui = runtime.get("ui")
    if isinstance(ui, dict):
        health["navigatorWebHost"] = _health_check(str(ui.get("healthUrl", "")))
        health["client"] = _health_check(str(ui.get("clientUrl", "")) + "/")
    value = {
        "status": "running" if all(running) and all(health.values()) else "stopped-or-unhealthy",
        "runtimePath": str(_runtime_path(state_dir)),
        "storage": runtime.get("storage"),
        "services": services,
        "ui": ui,
        "generation": runtime.get("generation"),
        "plugins": runtime.get("plugins", []),
        "processes": [
            {"name": row.get("name"), "pid": row.get("pid"), "running": running[index]}
            for index, row in enumerate(processes)
        ],
        "health": health,
    }
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0 if value["status"] == "running" else 1


def _stop(args: argparse.Namespace) -> int:
    """Stop only matching process groups and preserve all user data."""

    state_dir = _safe_state_dir(args.state_dir, create=False)
    lock = _acquire_lock(state_dir)
    try:
        runtime = _load_runtime(state_dir)
        if runtime.get("status") == "stopped":
            print(f"Trial is already stopped. Data remain in {state_dir}.")
            return 0
        records = list(reversed(runtime["processes"]))
        owned = [record for record in records if _process_matches(record)]
        for record in owned:
            try:
                os.killpg(int(record["processGroup"]), signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        for record in owned:
            while _process_matches(record) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _process_matches(record):
                try:
                    os.killpg(int(record["processGroup"]), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        runtime["status"] = "stopped"
        runtime["stoppedAt"] = _utc_now()
        _write_runtime(state_dir, runtime)
        print(f"Trial stopped. Product databases and artifacts remain in {state_dir}.")
        return 0
    finally:
        lock.close()


def _backup(args: argparse.Namespace) -> int:
    """Create a data-only backup after the Products have been stopped."""

    state_dir = _safe_state_dir(args.state_dir, create=False)
    runtime = _load_runtime(state_dir)
    if runtime.get("status") != "stopped" or any(
        _process_matches(record) for record in runtime["processes"]
    ):
        raise TrialLauncherError("stop the trial before creating a consistent data backup")
    output = (
        args.output.expanduser().absolute()
        if args.output is not None
        else state_dir.parent
        / "data-tools-trial-backups"
        / f"data-tools-trial-{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    )
    if output.is_symlink() or output.exists():
        raise TrialLauncherError(f"backup output already exists or is unsafe: {output}")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sources = [state_dir / "artifacts", state_dir / "catalyst", state_dir / "echo"]
    with tarfile.open(output, "x:gz") as archive:
        for path in sources:
            if path.is_symlink():
                raise TrialLauncherError(f"refusing to back up a symlink: {path}")
            if path.exists():
                archive.add(path, arcname=path.relative_to(state_dir).as_posix(), recursive=True)
    os.chmod(output, 0o600)
    print(f"Data backup written: {output}")
    return 0


def _clean(args: argparse.Namespace) -> int:
    """Remove trial state only after an explicit data-loss confirmation flag."""

    if not args.confirm_data_loss:
        raise TrialLauncherError("clean requires --confirm-data-loss")
    state_dir = _safe_state_dir(args.state_dir, create=False)
    runtime = _load_runtime(state_dir)
    if runtime.get("status") != "stopped" or any(
        _process_matches(record) for record in runtime["processes"]
    ):
        raise TrialLauncherError("stop all trial processes before clean")
    resolved_home = Path.home().resolve()
    if state_dir in {Path("/"), resolved_home} or state_dir in state_dir.parents:
        raise TrialLauncherError(f"refusing unsafe clean target: {state_dir}")
    shutil.rmtree(state_dir)
    print(f"Trial state and data removed: {state_dir}")
    return 0


def _add_state_argument(command: argparse.ArgumentParser) -> None:
    """Add the per-instance state directory option to a command."""

    command.add_argument("--state-dir", type=Path, default=_default_state_dir())


def build_parser() -> argparse.ArgumentParser:
    """Build the local operator command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="install local runtime inputs and start APIs")
    _add_state_argument(start)
    task_root = Path(__file__).resolve().parents[2]
    start.add_argument("--source-root", type=Path, default=task_root)
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--catalyst-port", type=int, default=8014)
    start.add_argument("--echo-port", type=int, default=8094)
    start.add_argument(
        "--generation-config",
        type=Path,
        help="JSON config for an already-resolved local model.provider.v1 connection",
    )
    start.add_argument(
        "--generation-binding-id",
        help=f"provider binding id (default: {DEFAULT_GENERATION_BINDING_ID})",
    )
    start.add_argument(
        "--with-ui",
        action="store_true",
        help="also start the current loopback Client Control, Vite, and Navigator Web Host",
    )
    start.add_argument("--navigator-root", type=Path)
    start.add_argument("--navigator-port", type=int, default=8100)
    start.add_argument("--client-port", type=int, default=5180)
    start.add_argument("--control-port", type=int, default=5280)
    start.add_argument(
        "--plugin-port-base",
        type=int,
        default=0,
        help="first local gRPC port; 0 lets the OS assign unique loopback ports",
    )
    start.add_argument("--allow-remote", action="store_true")
    start.add_argument("--startup-timeout", type=float, default=STARTUP_TIMEOUT_SECONDS)
    start.set_defaults(handler=_start)

    status = commands.add_parser("status", help="show local service health and process state")
    _add_state_argument(status)
    status.set_defaults(handler=_status)

    stop = commands.add_parser("stop", help="stop services and preserve persistent data")
    _add_state_argument(stop)
    stop.set_defaults(handler=_stop)

    backup = commands.add_parser("backup", help="archive databases and shared artifacts")
    _add_state_argument(backup)
    backup.add_argument("--output", type=Path)
    backup.set_defaults(handler=_backup)

    clean = commands.add_parser("clean", help="remove stopped trial data and local environments")
    _add_state_argument(clean)
    clean.add_argument("--confirm-data-loss", action="store_true")
    clean.set_defaults(handler=_clean)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one operator action and return an error without exposing credentials."""

    arguments = build_parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except TrialLauncherError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; launched processes were stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
