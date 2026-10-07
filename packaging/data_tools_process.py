"""Supervise one detached trial child while keeping logs redacted.

This helper owns the child output pipe after the launcher exits, so long-lived
services and the Client dev stack remain healthy while their logs are persisted.
它在launcher退出后继续采集脱敏日志，并可发布Plugin实际ready事件。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

TOKEN_ENV = "CYRENE_DATA_TOOLS_TOKEN"


def _publish_ready_event(path: Path, value: dict[str, Any]) -> None:
    """Atomically expose a real Plugin endpoint event to the launcher."""

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    """Run a child process and stream token-redacted output into its log."""

    try:
        separator = sys.argv.index("--")
    except ValueError:
        print("supervisor requires -- before the child command", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--ready-event-path", type=Path)
    options = parser.parse_args(sys.argv[1:separator])
    command = sys.argv[separator + 1 :]
    if not command:
        print("supervisor received an empty child command", file=sys.stderr)
        return 2

    options.log_path.parent.mkdir(parents=True, exist_ok=True)
    token = os.environ.get(TOKEN_ENV)
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        with options.log_path.open("a", encoding="utf-8") as log:
            log.write(f"could not start child process: {error}\n")
        return 127

    with options.log_path.open("a", encoding="utf-8", errors="replace") as log:
        assert child.stdout is not None
        for line in child.stdout:
            log.write(line.replace(token, "[redacted]") if token else line)
            log.flush()
            if options.ready_event_path is None:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("event") == "direct_plugin_ready":
                _publish_ready_event(options.ready_event_path, value)
                options.ready_event_path = None
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
