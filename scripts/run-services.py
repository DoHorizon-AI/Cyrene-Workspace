"""
Orchestration manager for starting and stopping the 6 Product services for Text Model Lifecycle V1 acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import urllib.request
import urllib.error

ENV_DIR = Path("/tmp/cyrene-acceptance-env")
LOGS_DIR = ENV_DIR / "logs"
PIDS_FILE = ENV_DIR / "pids.json"

SERVICES = [
    {
        "name": "catalyst",
        "port": 8014,
        "health_path": "/openapi.json",
        "cwd": "/home/baijin/Dev/Cyrene/Services/Cyrene-Catalyst",
        "cmd": [
            "uv", "run", "--project", "/home/baijin/Dev/Cyrene/Services/Cyrene-Catalyst",
            "python", "-m", "cyrene_catalyst"
        ],
        "env": {
            "CATALYST_PORT": "8014",
            "CATALYST_HOME": "/tmp/cyrene-acceptance-env/catalyst",
            "CYRENE_ARTIFACT_ROOT": "/tmp/cyrene-runtime-test/platform/artifacts",
            "CYRENE_YIELD_URL": "http://127.0.0.1:8092",
        },
    },
    {
        "name": "yield",
        "port": 8092,
        "health_path": "/openapi.json",
        "cwd": "/home/baijin/Dev/Cyrene/Services/Cyrene-Yield",
        "cmd": [
            "uv", "run", "--project", "/home/baijin/Dev/Cyrene/Services/Cyrene-Yield",
            "python", "-m", "cy_exec.training.product_cli",
            "--runtime-config", "/tmp/cyrene-runtime-test/platform/runtime.json",
            "--trainer-runtime-config", "/tmp/cyrene-runtime-test/trainer/runtime.json",
            "--state-directory", "/tmp/cyrene-acceptance-env/yield",
            "--reactor-url", "http://127.0.0.1:19300",
            "--reactor-token-env", "CYRENE_REACTOR_TOKEN",
            "--port", "8092",
        ],
        "env": {},
    },
    {
        "name": "reactor-host",
        "port": 19301,
        "health_path": "/docs",
        "cwd": "/home/baijin/Dev/Cyrene/Services/Cyrene-Reactor/product",
        "cmd": [
            "uv", "run", "--project", "/home/baijin/Dev/Cyrene/Services/Cyrene-Reactor/product",
            "python", "-m", "cyrene_reactor_product.cli", "host",
            "--config", "/tmp/cyrene-runtime-test/reactor/host.json",
            "--port", "19301",
        ],
        "env": {},
    },
    {
        "name": "reactor-control",
        "port": 19300,
        "health_path": "/docs",
        "cwd": "/home/baijin/Dev/Cyrene/Services/Cyrene-Reactor/product",
        "cmd": [
            "uv", "run", "--project", "/home/baijin/Dev/Cyrene/Services/Cyrene-Reactor/product",
            "python", "-m", "cyrene_reactor_product.cli", "control",
            "--config", "/tmp/cyrene-runtime-test/reactor/control.json",
            "--port", "19300",
        ],
        "env": {},
    },
    {
        "name": "exchange",
        "port": 8000,
        "health_path": "/docs",
        "cwd": "/home/baijin/Dev/Cyrene/Services/Cyrene-Exchange/product",
        "cmd": [
            "uv", "run", "--project", "/home/baijin/Dev/Cyrene/Services/Cyrene-Exchange/product",
            "python", "/home/baijin/Dev/Cyrene/Cyrene-Workspace/scripts/serve-exchange.py",
            "--database", "/tmp/cyrene-acceptance-env/exchange/exchange.sqlite3",
            "--port", "8000",
            "--control-token", "acceptance-exchange-control-token-12345678",
            "--allowed-bindings", "local-gpu,vllm-product",
        ],
        "env": {},
    },
    {
        "name": "navigator",
        "port": 8012,
        "health_path": "/openapi.json",
        "cwd": "/home/baijin/Dev/Cyrene/Services/Cyrene-Navigator",
        "cmd": [
            "uv", "run", "--project", "/home/baijin/Dev/Cyrene/Services/Cyrene-Navigator",
            "python", "/home/baijin/Dev/Cyrene/Services/Cyrene-Navigator/scripts/serve-persistence.py",
            "--database", "/tmp/cyrene-acceptance-env/navigator/sessions.sqlite3",
            "--principal-config", "/tmp/cyrene-acceptance-env/navigator/principal.json",
            "--port", "8012",
            "--artifact-root", "/tmp/cyrene-runtime-test/platform/artifacts",
            "--echo-url", "http://127.0.0.1:8094",
        ],
        "env": {
            "CYRENE_NAVIGATOR_TOKEN": "acceptance-navigator-token-12345678901234567890",
        },
    },
    {
        "name": "echo",
        "port": 8094,
        "health_path": "/openapi.json",
        "cwd": "/home/baijin/Dev/Cyrene/Services/Cyrene-Echo",
        "cmd": [
            "uv", "run", "--project", "/home/baijin/Dev/Cyrene/Services/Cyrene-Echo",
            "python", "-c", "from cyrene_echo.server import main; main()",
            "--database", "/tmp/cyrene-acceptance-env/echo/echo.sqlite3",
            "--artifact-root", "/tmp/cyrene-runtime-test/platform/artifacts",
            "--platform-artifacts",
            "--catalyst-url", "http://127.0.0.1:8014",
            "--port", "8094",
        ],
        "env": {},
    },
]


def wait_healthy(name: str, port: int, path: str, timeout: float = 30.0) -> bool:
    url = f"http://127.0.0.1:{port}{path}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HealthCheck"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if 200 <= resp.status < 400:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def start_all() -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("catalyst", "yield", "exchange", "navigator", "echo"):
        (ENV_DIR / sub).mkdir(parents=True, exist_ok=True)

    principal_file = ENV_DIR / "navigator" / "principal.json"
    if not principal_file.exists():
        principal_file.write_text(
            json.dumps({
                "principals": [
                    {
                        "token_env": "CYRENE_NAVIGATOR_TOKEN",
                        "actor_id": "acceptance-actor",
                        "workspace_ids": ["acceptance-workspace", "default"],
                        "can_takeover": True,
                    }
                ]
            }, indent=2),
            encoding="utf-8",
        )

    pids = {}
    if PIDS_FILE.exists():
        print("Pids file exists. Stopping previous services first...")
        stop_all()

    reactor_token_file = Path("/tmp/cyrene-runtime-test/reactor/private/control.token")
    reactor_token = reactor_token_file.read_text().strip() if reactor_token_file.is_file() else ""

    for s in SERVICES:
        name = s["name"]
        log_out = open(LOGS_DIR / f"{name}.stdout.log", "w", encoding="utf-8")
        log_err = open(LOGS_DIR / f"{name}.stderr.log", "w", encoding="utf-8")
        env = os.environ.copy()
        if reactor_token:
            env["CYRENE_REACTOR_TOKEN"] = reactor_token
        env.update(s["env"])
        print(f"Starting {name} on port {s['port']}...")
        proc = subprocess.Popen(
            s["cmd"],
            cwd=s["cwd"],
            env=env,
            stdout=log_out,
            stderr=log_err,
            preexec_fn=os.setsid,
        )
        pids[name] = proc.pid

    PIDS_FILE.write_text(json.dumps(pids, indent=2), encoding="utf-8")

    all_healthy = True
    for s in SERVICES:
        name = s["name"]
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

    print("All 7 Product services are READY.")
    return 0


def stop_all() -> int:
    if not PIDS_FILE.exists():
        print("No pids file found.")
        return 0

    try:
        pids = json.loads(PIDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pids = {}

    for name, pid in reversed(list(pids.items())):
        print(f"Stopping {name} (pid {pid})...")
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            print(f"Error terminating {name}: {exc}")

    time.sleep(2.0)

    for name, pid in pids.items():
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            pass

    if PIDS_FILE.exists():
        PIDS_FILE.unlink()
    print("All Product services stopped.")
    return 0


def status_all() -> int:
    if not PIDS_FILE.exists():
        print("No active services recorded.")
        return 1

    try:
        pids = json.loads(PIDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pids = {}

    all_alive = True
    for name, pid in pids.items():
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
