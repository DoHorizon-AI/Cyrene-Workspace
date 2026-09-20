"""
Orchestration manager for starting and stopping the 6 Product services for Text Model Lifecycle V1 acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CYRENE_ROOT = WORKSPACE_ROOT.parent
SERVICES_ROOT = CYRENE_ROOT / "Cyrene-Services"

DEV_HOME = Path(
    os.environ.get("CYRENE_DEV_HOME") or Path.home() / ".local" / "state" / "cyrene" / "dev"
).expanduser()
ENV_DIR = DEV_HOME / "services"
LOGS_DIR = DEV_HOME / "logs"
PIDS_FILE = DEV_HOME / "pids.json"
ARTIFACT_ROOT = DEV_HOME / "artifacts"
CREDENTIALS_FILE = DEV_HOME / "credentials.env"
PLUGINS_ROOT = Path(
    os.environ.get("CYRENE_PLUGINS_WORKTREE", CYRENE_ROOT / "Cyrene-Plugins-Official")
).expanduser()

_CREDENTIAL_KEYS = ("CYRENE_EXCHANGE_TOKEN", "CYRENE_NAVIGATOR_TOKEN")


def load_credentials() -> dict[str, str]:
    """Load or create the operator credentials shared with lifecycle commands.

    Generated tokens live in one mode-0600 file so `cyrene-dev up` can hand the
    lifecycle client exactly the credentials the Products were started with,
    instead of every script inventing its own constant.
    """

    if CREDENTIALS_FILE.is_file():
        loaded: dict[str, str] = {}
        for line in CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in _CREDENTIAL_KEYS and value.strip():
                loaded[key.strip()] = value.strip()
        if len(loaded) == len(_CREDENTIAL_KEYS):
            return loaded
    generated = {key: secrets.token_urlsafe(32) for key in _CREDENTIAL_KEYS}
    DEV_HOME.mkdir(parents=True, exist_ok=True)
    fd = os.open(CREDENTIALS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for key, value in generated.items():
            stream.write(f"{key}={value}\n")
    return generated


SERVICES = [
    {
        "name": "product-endpoints",
        "port": 0,
        "health_path": "",
        "cwd": str(WORKSPACE_ROOT),
        "cmd": [
            sys.executable,
            str(WORKSPACE_ROOT / "scripts/product-endpoints.py"),
            "up",
        ],
        "env": {},
        "optional": True,
    },
    {
        "name": "serving-runtime",
        "port": 19400,
        "health_path": "/healthz",
        "cwd": str(PLUGINS_ROOT / "plugins/serving/vllm-runtime"),
        "cmd": [
            sys.executable,
            str(PLUGINS_ROOT / "plugins/serving/vllm-runtime/vllm_runtime.py"),
            "serve",
            "--runtime-home",
            str(DEV_HOME / "reactor-serving"),
            "--artifact-root",
            str(ARTIFACT_ROOT),
            "--credential-file",
            str(DEV_HOME / "reactor/private/serving.token"),
            "--control-url",
            "http://127.0.0.1:19400",
            "--port",
            "19400",
        ],
        "env": {},
        "optional": True,
    },
    {
        "name": "catalyst",
        "port": 8014,
        "health_path": "/openapi.json",
        "cwd": str(SERVICES_ROOT / "Cyrene-Catalyst"),
        "cmd": [
            "uv",
            "run",
            "--project",
            str(SERVICES_ROOT / "Cyrene-Catalyst"),
            "python",
            "-m",
            "cyrene_catalyst",
        ],
        "env": {
            "CATALYST_PORT": "8014",
            "CATALYST_HOME": str(ENV_DIR / "catalyst"),
            "CYRENE_ARTIFACT_ROOT": str(ARTIFACT_ROOT),
            "CYRENE_YIELD_URL": "http://127.0.0.1:8092",
        },
    },
    {
        "name": "yield",
        "port": 8092,
        "health_path": "/openapi.json",
        "cwd": str(SERVICES_ROOT / "Cyrene-Yield"),
        "cmd": [
            "uv",
            "run",
            "--project",
            str(SERVICES_ROOT / "Cyrene-Yield"),
            "python",
            "-m",
            "cy_exec.training.product_cli",
            "--runtime-config",
            str(DEV_HOME / "platform" / "runtime.json"),
            "--trainer-runtime-config",
            str(DEV_HOME / "trainer" / "runtime.json"),
            "--state-directory",
            str(ENV_DIR / "yield"),
            "--reactor-url",
            "http://127.0.0.1:19300",
            "--reactor-token-env",
            "CYRENE_REACTOR_TOKEN",
            "--port",
            "8092",
        ],
        "env": {},
    },
    {
        "name": "reactor-control",
        "port": 19300,
        "health_path": "/docs",
        "cwd": str(SERVICES_ROOT / "Cyrene-Reactor/product"),
        "cmd": [
            "uv",
            "run",
            "--project",
            str(SERVICES_ROOT / "Cyrene-Reactor/product"),
            "python",
            "-m",
            "cyrene_reactor_product.cli",
            "control",
            "--config",
            str(DEV_HOME / "reactor" / "control.json"),
            "--port",
            "19300",
        ],
        "env": {},
    },
    {
        "name": "exchange",
        "port": 8000,
        "health_path": "/docs",
        "cwd": str(SERVICES_ROOT / "Cyrene-Exchange/product"),
        "cmd": [
            "uv",
            "run",
            "--project",
            str(SERVICES_ROOT / "Cyrene-Exchange/product"),
            "cyrene-exchange",
            "serve",
            "--database",
            str(ENV_DIR / "exchange" / "exchange.sqlite3"),
            "--port",
            "8000",
            "--control-token-env",
            "CYRENE_EXCHANGE_TOKEN",
            "--allowed-bindings",
            "local-gpu,vllm-product",
            "--provider-from-route-source",
            "--allowed-source-origin",
            "http://127.0.0.1:19300",
            "--source-token-env",
            "CYRENE_EXCHANGE_SOURCE_TOKEN",
        ],
        "env": {},
    },
    {
        "name": "navigator",
        "port": 8012,
        "health_path": "/openapi.json",
        "cwd": str(SERVICES_ROOT / "Cyrene-Navigator"),
        "cmd": [
            "uv",
            "run",
            "--project",
            str(SERVICES_ROOT / "Cyrene-Navigator"),
            "python",
            str(SERVICES_ROOT / "Cyrene-Navigator/scripts/serve-persistence.py"),
            "--database",
            str(ENV_DIR / "navigator" / "sessions.sqlite3"),
            "--principal-config",
            str(ENV_DIR / "navigator" / "principal.json"),
            "--port",
            "8012",
            "--artifact-root",
            str(ARTIFACT_ROOT),
            "--echo-url",
            "http://127.0.0.1:8094",
        ],
        "env": {},
    },
    {
        "name": "echo",
        "port": 8094,
        "health_path": "/openapi.json",
        "cwd": str(SERVICES_ROOT / "Cyrene-Echo"),
        "cmd": [
            "uv",
            "run",
            "--project",
            str(SERVICES_ROOT / "Cyrene-Echo"),
            "python",
            "-c",
            "from cyrene_echo.server import main; main()",
            "--database",
            str(ENV_DIR / "echo" / "echo.sqlite3"),
            "--artifact-root",
            str(ARTIFACT_ROOT),
            "--catalyst-url",
            "http://127.0.0.1:8014",
            "--port",
            "8094",
        ],
        "env": {},
    },
]


def wait_healthy(name: str, port: int, path: str, timeout: float = 30.0) -> bool:
    del name
    url = f"http://127.0.0.1:{port}{path}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HealthCheck"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if 200 <= resp.status < 400:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def materialize_reactor_config(credentials: dict[str, str]) -> Path:
    """Write Reactor's private control configuration and binding credentials.

    Reactor owns no local bootstrap any more: its serving backend is the
    Plugins-owned vLLM runtime, so the workspace only materializes the private
    files the Product needs and never invents product state.
    """

    reactor = DEV_HOME / "reactor"
    private = reactor / "private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    for name, value in (
        ("control.token", secrets.token_urlsafe(48)),
        ("serving.token", secrets.token_urlsafe(48)),
    ):
        marker = private / name
        if not marker.is_file():
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value)
    exchange_token = private / "exchange.token"
    fd = os.open(exchange_token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(credentials["CYRENE_EXCHANGE_TOKEN"])
    configuration = {
        "database_path": str(reactor / "reactor.sqlite3"),
        "credential_file": str(private / "control.token"),
        "public_base_url": "http://127.0.0.1:19300",
        "serving_bindings": [
            {
                "binding_id": "local-gpu",
                "control_url": "http://127.0.0.1:19400",
                "credential_file": str(private / "serving.token"),
            }
        ],
        "exchange_receivers": [
            {
                "receiver_id": "local-exchange",
                "control_url": "http://127.0.0.1:8000",
                "credential_file": str(exchange_token),
                "allowed_binding_ids": ["local-gpu"],
            }
        ],
    }
    target = reactor / "control.json"
    pending = target.with_suffix(".pending")
    pending.write_text(json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pending.replace(target)
    return target


def load_endpoint_references() -> dict[str, str]:
    """Read the DirectPlugin connection references the supervisor published."""

    target = DEV_HOME / "endpoints.json"
    if not target.is_file():
        return {}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def _read_pids() -> dict[str, int]:
    try:
        value = json.loads(PIDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {name: int(pid) for name, pid in value.items()}


def _terminate(pids: dict[str, int], sig: int) -> None:
    for name, pid in pids.items():
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            continue
        except OSError as exc:
            print(f"Error terminating {name}: {exc}")


def start_all() -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("catalyst", "yield", "exchange", "navigator", "echo", "reactor"):
        (ENV_DIR / sub).mkdir(parents=True, exist_ok=True)

    principal_file = ENV_DIR / "navigator" / "principal.json"
    if not principal_file.exists():
        principal_file.write_text(
            json.dumps(
                {
                    "principals": [
                        {
                            "token_env": "CYRENE_NAVIGATOR_TOKEN",
                            "actor_id": "acceptance-actor",
                            "workspace_ids": ["acceptance-workspace", "default"],
                            "can_takeover": True,
                        }
                    ]
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    pids: dict[str, int] = {}
    if PIDS_FILE.exists():
        print("Pids file exists. Stopping previous services first...")
        stop_all()

    credentials = load_credentials()
    materialize_reactor_config(credentials)
    endpoint_references = load_endpoint_references()
    reactor_token_file = DEV_HOME / "reactor" / "private" / "control.token"
    reactor_token = reactor_token_file.read_text().strip() if reactor_token_file.is_file() else ""

    for s in SERVICES:
        name = s["name"]
        env = os.environ.copy()
        if reactor_token:
            env["CYRENE_REACTOR_TOKEN"] = reactor_token
            env["CYRENE_EXCHANGE_SOURCE_TOKEN"] = reactor_token
        env.update(s["env"])
        env.update(credentials)
        env.update(endpoint_references)
        if s.get("optional") and not Path(s["cmd"][1]).exists():
            print(f"Skipping {name}: {s['cmd'][1]} is not present")
            continue
        print(f"Starting {name} on port {s['port']}...")
        with (
            (LOGS_DIR / f"{name}.stdout.log").open("w", encoding="utf-8") as log_out,
            (LOGS_DIR / f"{name}.stderr.log").open("w", encoding="utf-8") as log_err,
        ):
            proc = subprocess.Popen(
                s["cmd"],
                cwd=s["cwd"],
                env=env,
                stdout=log_out,
                stderr=log_err,
                start_new_session=True,
            )
        pids[name] = proc.pid

    PIDS_FILE.write_text(json.dumps(pids, indent=2), encoding="utf-8")

    all_healthy = True
    for s in SERVICES:
        name = s["name"]
        if name not in pids or not s["health_path"]:
            continue
        print(f"Waiting for {name} on port {s['port']}...", end="", flush=True)
        if wait_healthy(name, s["port"], s["health_path"]):
            print(" READY")
        else:
            print(f" FAILED (check {LOGS_DIR / f'{name}.stderr.log'})")
            all_healthy = False

    if not all_healthy:
        print("Not all services started successfully. Aborting.")
        stop_all()
        return 1

    print("All Product services are READY.")
    print(f"Operator credentials: {CREDENTIALS_FILE}")
    print("For lifecycle commands run: set -a; . " + str(CREDENTIALS_FILE) + "; set +a")
    return 0


def stop_all() -> int:
    if not PIDS_FILE.exists():
        print("No pids file found.")
        return 0

    pids = _read_pids()
    for name in reversed(list(pids)):
        print(f"Stopping {name} (pid {pids[name]})...")

    _terminate(pids, signal.SIGTERM)
    time.sleep(2.0)
    _terminate(pids, signal.SIGKILL)

    PIDS_FILE.unlink(missing_ok=True)
    print("All Product services stopped.")
    return 0


def status_all() -> int:
    if not PIDS_FILE.exists():
        print("No active services recorded.")
        return 1

    all_alive = True
    for name, pid in _read_pids().items():
        try:
            os.kill(pid, 0)
            status = "RUNNING"
        except ProcessLookupError:
            status = "DEAD"
            all_alive = False
        print(f"{name:15}: PID {pid:<6} [{status}]")

    return 0 if all_alive else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Cyrene Services Runner")
    parser.add_argument("command", choices=["start", "stop", "status", "restart"])
    args = parser.parse_args()

    if args.command == "start":
        sys.exit(start_all())
    elif args.command == "stop":
        sys.exit(stop_all())
    elif args.command == "status":
        sys.exit(status_all())
    elif args.command == "restart":
        stop_all()
        time.sleep(1.0)
        sys.exit(start_all())


if __name__ == "__main__":
    main()
