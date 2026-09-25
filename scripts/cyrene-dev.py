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

中文:一个统一入口组合两个已经包含经验证逻辑的 owner:reference-runtime.py 协调 Platform/trainer/serving runtimes,run-services.py 负责监管 Product APIs。此工具只供开发者使用,不是任何 Product 的依赖。
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
}

RUNTIME_COMMANDS = {"up": "bootstrap", "status": "status", "down": "down"}
SERVICE_COMMANDS = {"up": "start", "status": "status", "down": "stop"}


def dev_home() -> Path:
    """Return the single canonical data root shared with the two owners.

    中文:返回由两个 owner 共同使用的唯一规范数据根目录。
    """

    configured = os.environ.get("CYRENE_DEV_HOME")
    return Path(configured).expanduser() if configured else DEFAULT_DEV_HOME


def default_worktrees() -> dict[str, str]:
    """Derive the sibling checkouts so a fresh terminal needs no exports.

    An explicit environment variable always wins, and a sibling that is not a
    git checkout is left unset so the owning runtime reports the exact missing
    repository instead of silently pointing at an empty directory.

    中文:推导相邻 checkout 路径,使新终端无需手工导出环境变量。显式环境变量始终优先;如果相邻路径不是 git checkout,就不设置该变量,让所属 runtime 准确报告缺少哪个仓库,而不是静默指向空目录。
    """

    derived: dict[str, str] = {}
    for variable, candidate in DEFAULT_WORKTREES.items():
        if os.environ.get(variable):
            continue
        if (candidate / ".git").exists():
            derived[variable] = str(candidate)
    return derived


def guard_home(home: Path) -> Path:
    """Refuse any data root that could destroy unrelated user or source data.

    中文:拒绝可能破坏其他用户数据或源码的 data root。
    """

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
    """Create the managed tree without ever removing an existing path.

    中文:创建受管目录树,但绝不删除已存在的路径。
    """

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
        # 中文:服务依赖运行时,因此应先停止服务;如果某个运行时卡住,
        # 中文:Services 依赖 runtimes,因此先释放 Services;若 runtime teardown 卡住,
        # runtime teardown must never leave Product processes holding ports.
        # 中文:拆除运行时后也不得遗留仍占用端口的 Product 进程。
        # 中文:也不能让 Product 进程继续占用端口。
        return _teardown(arguments.scope, environment)

    exit_code = 0
    if arguments.scope in ("all", "runtimes"):
        runtime_arguments = [RUNTIME_COMMANDS[arguments.command], "--profile", arguments.profile]
        if arguments.command == "up" and arguments.no_build:
            runtime_arguments.append("--no-build")
        exit_code = _delegate(RUNTIME_SCRIPT, runtime_arguments, environment)
        if exit_code and arguments.command == "up":
            # Starting Product APIs without their runtimes only produces
            # 中文:未启动对应运行时就启动 Product API,只会形成
            # 中文:若没有 runtimes 就启动 Product APIs,只会得到
            # half-configured services, so stop here and report the cause.
            # 中文:配置不完整的服务,因此应在此停止并报告原因。
            # 中文:配置不完整的服务,因此应在此停止并报告原因。
            print(
                "Runtime bootstrap failed; not starting the Product services.",
                file=sys.stderr,
            )
            return exit_code
    if arguments.scope in ("all", "services"):
        exit_code |= _delegate(SERVICES_SCRIPT, [SERVICE_COMMANDS[arguments.command]], environment)
    return exit_code


def _teardown(scope: str, environment: dict[str, str]) -> int:
    """Release Product services before the runtimes they depend on.

    中文:先释放依赖方 Product Services,再释放其所依赖的 runtimes。
    """

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
