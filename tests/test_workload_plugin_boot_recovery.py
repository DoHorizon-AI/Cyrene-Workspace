"""Tests for the supervised Catalyst Package Runtime boot recovery hook.

中文：验证 Catalyst 预启动恢复失败关闭，并验证官方 DEB 包含可追溯 helper。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
HELPER = PACKAGING / "workload_plugin_boot_recovery.py"
BUILD_SCRIPT = PACKAGING / "build-deb.sh"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("workload_plugin_boot_recovery_test", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Updater:
    """Return a caller-selected public recovery summary for helper tests."""

    def __init__(
        self, result: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.result = result or {
            "status": "recovered",
            "workloadId": "catalyst",
            "reason": None,
            "recoveredBindings": 2,
            "projectionChanged": True,
        }
        self.error = error
        self.workloads: list[str] = []

    def recover_workload_runtime_connections(self, workload_id: str) -> dict[str, Any]:
        self.workloads.append(workload_id)
        if self.error is not None:
            raise self.error
        return self.result


def test_boot_helper_accepts_recovery_and_explicit_safe_skip() -> None:
    helper = _load_helper()
    for result in (
        {
            "status": "recovered",
            "workloadId": "catalyst",
            "reason": None,
            "recoveredBindings": 1,
            "projectionChanged": True,
        },
        {
            "status": "skipped",
            "workloadId": "catalyst",
            "reason": "no-managed-bindings",
            "recoveredBindings": 0,
            "projectionChanged": False,
        },
    ):
        updater = _Updater(result)
        stdout = io.StringIO()
        stderr = io.StringIO()
        assert (
            helper.main(
                [], updater_factory=lambda updater=updater: updater, stdout=stdout, stderr=stderr
            )
            == 0
        )
        assert updater.workloads == ["catalyst"]
        assert json.loads(stdout.getvalue()) == {
            key: result[key]
            for key in ("status", "workloadId", "reason", "recoveredBindings", "projectionChanged")
        }
        assert stderr.getvalue() == ""


def test_boot_helper_fails_closed_without_logging_exception_details() -> None:
    helper = _load_helper()
    updater = _Updater(error=RuntimeError("secret path or connection ref"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert helper.main([], updater_factory=lambda: updater, stdout=stdout, stderr=stderr) == 1
    assert stdout.getvalue() == ""
    failure = json.loads(stderr.getvalue())
    assert failure == {
        "status": "failed",
        "workloadId": "catalyst",
        "errorCode": "BOOT_RECOVERY_FAILED",
        "retryable": False,
    }
    assert "secret" not in stderr.getvalue()


def test_deb_stages_root_executable_helper_and_source_file_digest(tmp_path: Path) -> None:
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    start = source.index('cp "${SCRIPT_DIR}/workload_plugin_boot_recovery.py"')
    marker = 'chmod 644 "${STAGE_DIR}/usr/share/cyrene/workload-plugin-boot-recovery-v1.json"'
    end = source.index(marker, start) + len(marker)
    staging_block = source[start:end]
    stage_root = tmp_path / "stage"
    (stage_root / "usr/lib/cyrene/scripts").mkdir(parents=True)
    (stage_root / "usr/share/cyrene").mkdir(parents=True)

    subprocess.run(
        ["bash", "-eu", "-c", staging_block],
        env={**os.environ, "SCRIPT_DIR": str(PACKAGING), "STAGE_DIR": str(stage_root)},
        check=True,
        capture_output=True,
        text=True,
    )

    staged = stage_root / "usr/lib/cyrene/scripts/workload_plugin_boot_recovery.py"
    manifest_path = stage_root / "usr/share/cyrene/workload-plugin-boot-recovery-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert staged.read_bytes() == HELPER.read_bytes()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o755
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o644
    assert manifest == {
        "schemaVersion": 1,
        "manifestSourceFiles": {
            "packaging/workload_plugin_boot_recovery.py": {
                "installedPath": "usr/lib/cyrene/scripts/workload_plugin_boot_recovery.py",
                "sha256": "sha256:" + hashlib.sha256(HELPER.read_bytes()).hexdigest(),
            }
        },
    }


def test_catalyst_unit_runs_root_recovery_after_runtime_without_stop_coupling() -> None:
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    start = source.index("# cyrene-catalyst.service")
    end = source.index("\nEOF", start)
    unit = source[start:end]

    assert "Wants=cyrene-package-runtime.service" in unit
    assert "After=cyrene-runtime-maintenance.service" in unit
    assert "After=cyrene-package-runtime.service" in unit
    assert "Requires=cyrene-package-runtime.service" not in unit
    assert "BindsTo=cyrene-package-runtime.service" not in unit
    assert (
        "ExecStartPre=+/opt/cyrene/python/3.12.14/bin/python3.12 -sE "
        "/usr/lib/cyrene/scripts/workload_plugin_boot_recovery.py --workload catalyst"
    ) in unit
