"""Recover Catalyst Package Runtime connections before the Product starts.

The systemd pre-start hook delegates all ownership, RuntimeStatus, and
RecoverBinding decisions to the installed ComponentUpdater contract.
中文：在 Catalyst 启动前恢复受管插件连接，并由 ComponentUpdater 负责状态校验。
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import Any, TextIO

WORKLOAD_ID = "catalyst"
SUCCESS_STATUSES = frozenset({"recovered", "skipped"})


def recover_workload_connections(updater: Any, workload_id: str = WORKLOAD_ID) -> dict[str, Any]:
    """Call and validate the updater's public boot-recovery result.

    A skipped result is successful only when the updater explicitly identifies
    a safe first-install or maintenance-hold condition. All other uncertainty
    propagates as an error so systemd prevents Catalyst from starting.

    Args:
        updater: Installed ``ComponentUpdater`` instance.
        workload_id: Fixed workload identity selected by the packaged unit.
    Returns:
        A redacted recovery summary safe for the system journal.
    Raises:
        RuntimeError: If the updater omits or malforms its recovery result.
    """

    if workload_id != WORKLOAD_ID:
        raise RuntimeError("only Catalyst boot recovery is supported")
    result = updater.recover_workload_runtime_connections(workload_id)
    if (
        not isinstance(result, dict)
        or result.get("status") not in SUCCESS_STATUSES
        or result.get("workloadId") != workload_id
        or type(result.get("recoveredBindings")) is not int
        or result["recoveredBindings"] < 0
        or type(result.get("projectionChanged")) is not bool
    ):
        raise RuntimeError("ComponentUpdater returned an invalid boot-recovery result")
    reason = result.get("reason")
    if result["status"] == "skipped":
        if not isinstance(reason, str) or not reason or len(reason) > 200:
            raise RuntimeError("ComponentUpdater omitted the safe-skip reason")
    elif reason is not None:
        raise RuntimeError("ComponentUpdater returned an unexpected recovery reason")

    # Emit only nonsecret control-plane fields; connection references and
    # credential paths never enter journald.
    return {
        "status": result["status"],
        "workloadId": workload_id,
        "reason": reason,
        "recoveredBindings": result["recoveredBindings"],
        "projectionChanged": result["projectionChanged"],
    }


def main(
    argv: list[str] | None = None,
    *,
    updater_factory: Any | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the fixed Catalyst pre-start recovery operation.

    Args:
        argv: Optional command-line arguments, primarily for focused tests.
        updater_factory: Test seam; production loads the installed updater.
        stdout: Optional output stream.
        stderr: Optional error stream.
    Returns:
        Zero after a verified recovery or explicit safe skip; nonzero on error.
    """

    parser = argparse.ArgumentParser(prog="workload-plugin-boot-recovery")
    parser.add_argument("--workload", choices=(WORKLOAD_ID,), default=WORKLOAD_ID)
    arguments = parser.parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    try:
        if updater_factory is None:
            updates = importlib.import_module("component_updates")
            updater_factory = updates.ComponentUpdater
        updater = updater_factory()
        summary = recover_workload_connections(updater, arguments.workload)
    except Exception as error:  # noqa: BLE001 - every recovery error must fail closed.
        code = getattr(error, "code", "BOOT_RECOVERY_FAILED")
        retryable = getattr(error, "retryable", False)
        errors.write(
            json.dumps(
                {
                    "status": "failed",
                    "workloadId": arguments.workload,
                    "errorCode": str(code),
                    "retryable": retryable is True,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        errors.flush()
        return 1
    output.write(json.dumps(summary, separators=(",", ":")) + "\n")
    output.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
