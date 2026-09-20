"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: cyrene-dev developer orchestrator                           │
│ Role: init / up / status / down for the local multi-repository stack│
│ 模块职责：工作区内部开发编排器；统一入口、统一数据目录、统一清理。          │
└─────────────────────────────────────────────────────────────────────┘

One entrypoint composes the two owners that already hold the tested logic:
``reference-runtime.py`` coordinates the Platform/trainer/serving runtimes and
``run-services.py`` supervises the Product APIs.  Only developers run this tool;
no Product depends on it. | 本工具仅供开发联调，不属于任何产品的运行依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CYRENE_ROOT = WORKSPACE_ROOT.parent
RUNTIME_SCRIPT = WORKSPACE_ROOT / "scripts" / "reference-runtime.py"
SERVICES_SCRIPT = WORKSPACE_ROOT / "scripts" / "run-services.py"

DEFAULT_DEV_HOME = Path.home() / ".local" / "state" / "cyrene" / "dev"
SCOPES = ("all", "runtimes", "services")

DEFAULT_WORKTREES = {
    "CYRENE_PLATFORM_WORKTREE": CYRENE_ROOT / "Cyrene-Platform",
    "CYRENE_YIELD_WORKTREE": CYRENE_ROOT / "Cyrene-Services" / "Cyrene-Yield",
    "CYRENE_REACTOR_WORKTREE": CYRENE_ROOT / "Cyrene-Services" / "Cyrene-Reactor",
}

RUNTIME_COMMANDS = {"up": "bootstrap", "status": "status", "down": "down"}
SERVICE_COMMANDS = {"up": "start", "status": "status", "down": "stop"}


def dev_home() -> Path:
    """Return the single canonical data root shared with the two owners."""

    configured = os.environ.get("CYRENE_DEV_HOME")
    return Path(configured).expanduser() if configured else DEFAULT_DEV_HOME


def default_worktrees() -> dict[str, str]:
    """Derive the sibling checkouts so a fresh terminal needs no exports.

    An explicit environment variable always wins, and a sibling that is not a
    git checkout is left unset so the owning runtime reports the exact missing
    repository instead of silently pointing at an empty directory.
    """

    derived: dict[str, str] = {}
    for variable, candidate in DEFAULT_WORKTREES.items():
        if os.environ.get(variable):
            continue
        if (candidate / ".git").exists():
            derived[variable] = str(candidate)
    return derived


def guard_home(home: Path) -> Path:
    """Refuse any data root that could destroy unrelated user or source data."""

    if not home.is_absolute():
        raise ValueError("CYRENE_DEV_HOME must be an absolute path below the filesystem root")
    resolved = home.expanduser().resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError("CYRENE_DEV_HOME must not be the filesystem root or the home directory")
    for protected in (CYRENE_ROOT.resolve(), Path.home().resolve()):
        if protected.is_relative_to(resolved):
            raise ValueError(f"CYRENE_DEV_HOME must not contain {protected}")
    return resolved


def prepare_directories(home: Path) -> None:
    """Create the managed tree without ever removing an existing path."""

    for relative in ("platform", "trainer", "reactor", "services", "logs", "artifacts"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)


def _delegate(script: Path, arguments: list[str], environment: dict[str, str]) -> int:
    command = [sys.executable, str(script), *arguments]
    return subprocess.run(command, check=False, env=environment, timeout=7200).returncode


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Cyrene local development orchestrator")
    value.add_argument("command", choices=("init", "up", "status", "down"))
    value.add_argument("--scope", choices=SCOPES, default="all")
    value.add_argument(
        "--dev-home",
        type=Path,
        default=dev_home(),
        help="Canonical data root protected by this orchestrator",
    )
    value.add_argument(
        "--profile",
        choices=("NATIVE_LINUX_PROFILE", "WSL_DEV_PROFILE"),
        default="NATIVE_LINUX_PROFILE",
    )
    value.add_argument("--no-build", action="store_true")
    return value


def main() -> int:
    arguments = parser().parse_args()
    try:
        home = guard_home(arguments.dev_home)
    except ValueError as exc:
        print(json.dumps({"status": "FAILED", "code": str(exc)}, sort_keys=True))
        return 1
    if arguments.command == "init":
        prepare_directories(home)
        print(json.dumps({"status": "READY", "devHome": str(home)}, indent=2))
        return 0

    prepare_directories(home)
    environment = dict(os.environ)
    environment["CYRENE_DEV_HOME"] = str(home)
    environment["CYRENE_RUNTIME_HOME"] = str(home)
    environment["CYRENE_DEV_PROFILE"] = arguments.profile
    environment.update(default_worktrees())

    if arguments.command == "down":
        # Services depend on the runtimes, so they are released first; a stuck
        # runtime teardown must never leave Product processes holding ports.
        return _teardown(arguments.scope, environment)

    exit_code = 0
    if arguments.scope in ("all", "runtimes"):
        runtime_arguments = [RUNTIME_COMMANDS[arguments.command], "--profile", arguments.profile]
        if arguments.command == "up" and arguments.no_build:
            runtime_arguments.append("--no-build")
        exit_code = _delegate(RUNTIME_SCRIPT, runtime_arguments, environment)
        if exit_code and arguments.command == "up":
            # Starting Product APIs without their runtimes only produces
            # half-configured services, so stop here and report the cause.
            print(
                "Runtime bootstrap failed; not starting the Product services.",
                file=sys.stderr,
            )
            return exit_code
    if arguments.scope in ("all", "services"):
        exit_code |= _delegate(SERVICES_SCRIPT, [SERVICE_COMMANDS[arguments.command]], environment)
    return exit_code


def _teardown(scope: str, environment: dict[str, str]) -> int:
    """Release Product services before the runtimes they depend on."""

    exit_code = 0
    if scope in ("all", "services"):
        exit_code |= _delegate(SERVICES_SCRIPT, [SERVICE_COMMANDS["down"]], environment)
    if scope in ("all", "runtimes"):
        exit_code |= _delegate(
            RUNTIME_SCRIPT,
            [RUNTIME_COMMANDS["down"], "--profile", environment["CYRENE_DEV_PROFILE"]],
            environment,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
