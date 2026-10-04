"""Guard package builder setup against precreating locked Python outputs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = WORKSPACE_ROOT / "packaging/build-deb.sh"
RUNTIME_LOCK = WORKSPACE_ROOT / "packaging/python-runtime.lock.json"


def test_deb_builder_leaves_locked_python_outputs_for_stage_helper(tmp_path: Path) -> None:
    """The fresh package tree must not occupy outputs owned by python_runtime.prepare."""

    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    start = script.index("# Directory structure\n")
    end = script.index("# Stage the pinned private CPython", start)
    setup = script[start:end]
    stage_root = tmp_path / "deb-stage"
    stage_root.mkdir()
    subprocess.run(
        ["bash", "-eu", "-c", setup],
        env={**os.environ, "STAGE_DIR": str(stage_root)},
        check=True,
    )

    lock = json.loads(RUNTIME_LOCK.read_text(encoding="utf-8"))
    python_root = PurePosixPath(lock["python"]["installRoot"].lstrip("/"))
    payload_root = PurePosixPath(lock["payload"]["lockPath"]).parent
    uv_path = PurePosixPath(lock["buildResolver"]["installedPath"].lstrip("/"))
    uv_root = PurePosixPath(*uv_path.parts[:-2])
    managed_outputs = (python_root, payload_root, uv_root)

    for relative in managed_outputs:
        assert not (stage_root / Path(*relative.parts)).exists()
    assert (stage_root / "usr/share/cyrene").is_dir()
