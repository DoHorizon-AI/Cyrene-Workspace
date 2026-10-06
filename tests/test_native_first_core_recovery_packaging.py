"""Regression tests for staging the Core successor helper in the DEB.

中文：验证后继恢复 helper 随 native Core bootstrap 安装到固定路径。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = WORKSPACE_ROOT / "packaging/build-deb.sh"
PACKAGING_DIR = WORKSPACE_ROOT / "packaging"
SUCCESSOR_SOURCE = PACKAGING_DIR / "native_core_successor.py"
SUCCESSOR_DESTINATION = Path("usr/lib/cyrene/scripts/native_core_successor.py")


def test_deb_builder_stages_core_successor_helper_readable_by_runtime(
    tmp_path: Path,
) -> None:
    """Stage the exact helper bytes beside core bootstrap with mode 0644.

    中文：DEB 应复制源文件字节，并以运行时可读的 0644 权限安装。
    """

    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    copy_start = script.index('cp "${SCRIPT_DIR}/native_core_successor.py"')
    core_copy = script.index('cp "${SCRIPT_DIR}/native_core_bootstrap.py"')
    assert copy_start > core_copy
    chmod_line = 'chmod 644 "${STAGE_DIR}/usr/lib/cyrene/scripts/native_core_successor.py"'
    assert script.count(chmod_line) == 1
    copy_end = script.index("\n", script.index(chmod_line, copy_start))
    copy_block = script[copy_start:copy_end]

    assert SUCCESSOR_SOURCE.is_file()
    assert not SUCCESSOR_SOURCE.is_symlink()
    stage_root = tmp_path / "deb-stage"
    (stage_root / SUCCESSOR_DESTINATION.parent).mkdir(parents=True)
    subprocess.run(
        ["bash", "-eu", "-c", copy_block],
        env={
            **os.environ,
            "SCRIPT_DIR": str(PACKAGING_DIR),
            "STAGE_DIR": str(stage_root),
        },
        check=True,
    )

    staged_helper = stage_root / SUCCESSOR_DESTINATION
    assert staged_helper.read_bytes() == SUCCESSOR_SOURCE.read_bytes()
    assert stat.S_IMODE(staged_helper.stat().st_mode) == 0o644
