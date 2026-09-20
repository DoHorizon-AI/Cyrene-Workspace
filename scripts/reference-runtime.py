"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: Cyrene Text Lifecycle V1 reference runtime                 │
│ Role: Coordinate canonical Platform and trainer bootstrap.          │
│ 模块职责：统一协调 Platform 与训练运行时，隐藏内部路径拓扑。               │
└─────────────────────────────────────────────────────────────────────┘

Serving is owned by the Plugins vLLM runtime and is supervised by
``run-services.py``; this coordinator deliberately starts no serving component.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

PROFILE = "CYRENE_TEXT_LIFECYCLE_V1_LOCAL_GPU"


def _bounded_home(value: Path | None) -> Path:
    if value is None or not value.expanduser().is_absolute():
        raise ValueError("CYRENE_RUNTIME_HOME_REQUIRED")
    home = value.expanduser().resolve()
    if home == Path(home.anchor):
        raise ValueError("CYRENE_RUNTIME_HOME_INVALID")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    return home


def _repository(variable: str) -> Path:
    value = os.environ.get(variable)
    if not value:
        raise ValueError(variable + "_REQUIRED")
    path = Path(value).expanduser().resolve()
    if not (path / ".git").exists():
        raise ValueError(variable + "_INVALID")
    return path


def _invoke(command: list[str], code: str) -> dict[str, Any]:
    if not Path(command[0]).is_file():
        raise ValueError(code + "_ABSENT: " + command[0])
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=7200)
    try:
        value = json.loads(result.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError(code) from exc
    if (
        result.returncode
        or not isinstance(value, dict)
        or value.get("status") not in {"READY", "DOWN"}
    ):
        failure = value.get("code") if isinstance(value, dict) else None
        raise ValueError(str(failure or code))
    return value


def _write(path: Path, value: Any) -> None:
    pending = path.with_suffix(".pending")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(pending, path)
    path.chmod(0o600)


def _commands(args: argparse.Namespace, home: Path) -> dict[str, list[str]]:
    platform = _repository("CYRENE_PLATFORM_WORKTREE")
    yield_repository = _repository("CYRENE_YIELD_WORKTREE")
    platform_home = home / "platform"
    trainer_home = home / "trainer"
    runtime = str(platform / "tooling/runtime/cyrene-runtime")
    trainer = str(yield_repository / "trainer-runtime/cyrene-trainer-runtime")
    return {
        "platformUp": [
            runtime,
            "up",
            "--runtime-home",
            str(platform_home),
            "--profile",
            args.profile,
            *(["--no-build"] if args.no_build else []),
        ],
        "platformStatus": [runtime, "status", "--runtime-home", str(platform_home)],
        "platformDown": [runtime, "down", "--runtime-home", str(platform_home)],
        "trainerBootstrap": [trainer, "bootstrap", "--runtime-home", str(trainer_home)],
        "trainerStatus": [trainer, "status", "--runtime-home", str(trainer_home)],
        "trainerDown": [trainer, "down", "--runtime-home", str(trainer_home)],
    }


def _release_pid_files(runtime_home: Path) -> list[int]:
    """Terminate and remove process ids recorded inside one runtime home."""

    released: list[int] = []
    if not runtime_home.is_dir():
        return released
    for marker in sorted(runtime_home.rglob("*.pid")):
        try:
            pid = int(marker.read_text().strip())
        except (OSError, ValueError):
            marker.unlink(missing_ok=True)
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        except OSError:
            pass
        released.append(pid)
        marker.unlink(missing_ok=True)
    return released


def _public(components: dict[str, dict[str, Any]], mode: str, status: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "profile": PROFILE,
        "runtimeMode": mode,
        "status": status,
        "components": {
            name: {
                "profile": value.get("profile"),
                "status": value.get("status"),
                **(
                    {"teardown": value["teardown"], "releasedPids": value["releasedPids"]}
                    if value.get("teardown")
                    else {}
                ),
            }
            for name, value in components.items()
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    home = _bounded_home(args.runtime_home)
    commands = _commands(args, home)
    if args.command == "down":
        platform = _invoke(commands["platformDown"], "PLATFORM_RUNTIME_DOWN_FAILED")
        components: dict[str, dict[str, Any]] = {"platform": platform}
        for name in ("trainer",):
            command = commands[name + "Down"]
            teardown: dict[str, Any] = {"status": "DOWN"}
            if Path(command[0]).is_file():
                try:
                    components[name] = _invoke(command, name.upper() + "_RUNTIME_DOWN_FAILED")
                    continue
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    teardown["code"] = str(exc).split(":", 1)[0]
            teardown["teardown"] = "PID_RELEASE"
            teardown["releasedPids"] = _release_pid_files(home / name)
            components[name] = teardown
        result = _public(components, args.profile, "DOWN")
        _write(home / "reference-runtime.json", result)
        return result
    if args.command == "status":
        components = {
            "platform": _invoke(commands["platformStatus"], "PLATFORM_RUNTIME_STATUS_FAILED"),
            "trainer": _invoke(commands["trainerStatus"], "TRAINER_RUNTIME_STATUS_FAILED"),
        }
        mode = str(components["platform"].get("runtimeMode") or args.profile)
        result = _public(components, mode, "READY")
        _write(home / "reference-runtime.json", result)
        return result

    platform_started = False
    try:
        platform = _invoke(commands["platformUp"], "PLATFORM_RUNTIME_BOOTSTRAP_FAILED")
        platform_started = True
        trainer = _invoke(commands["trainerBootstrap"], "TRAINER_RUNTIME_BOOTSTRAP_FAILED")
        components = {"platform": platform, "trainer": trainer}
        mode = str(platform.get("runtimeMode") or args.profile)
        private = {
            **_public(components, mode, "READY"),
            "platformRuntimeConfig": str(home / "platform" / "runtime.json"),
            "trainerRuntimeConfig": str(home / "trainer" / "runtime.json"),
        }
        _write(home / "reference-runtime.json", private)
        return _public(components, mode, "READY")
    except (OSError, ValueError, subprocess.SubprocessError):
        if platform_started:
            try:
                _invoke(commands["platformDown"], "PLATFORM_RUNTIME_DOWN_FAILED")
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        raise


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Cyrene text lifecycle reference runtime")
    value.add_argument("command", choices=("bootstrap", "status", "down"))
    value.add_argument(
        "--runtime-home",
        type=Path,
        default=Path(os.environ["CYRENE_RUNTIME_HOME"])
        if os.environ.get("CYRENE_RUNTIME_HOME")
        else None,
    )
    value.add_argument(
        "--profile",
        choices=("NATIVE_LINUX_PROFILE", "WSL_DEV_PROFILE"),
        default="NATIVE_LINUX_PROFILE",
    )
    value.add_argument("--no-build", action="store_true")
    return value


def main() -> int:
    try:
        result = run(parser().parse_args())
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAILED", "code": str(exc).split(":", 1)[0]}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
