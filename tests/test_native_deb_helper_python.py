"""Guard native DEB helper selection against host-Python version drift."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = WORKSPACE_ROOT / "packaging/build-deb.sh"


def _helper_selection_block() -> str:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    start = script.index("# Use the workflow-selected pinned helper interpreter")
    end = script.index('mkdir -p "${OUTPUT_DIR}"', start)
    return script[start:end]


def _fake_python(directory: Path, version: str) -> Path:
    executable = directory / "python3"
    directory.mkdir(parents=True, exist_ok=True)
    executable.write_text(
        f"#!/bin/sh\n[ \"$1\" = \"-c\" ] || exit 9\nprintf '%s\\n' '{version}'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _run_selection(tmp_path: Path, version: str) -> subprocess.CompletedProcess[str]:
    binary_dir = tmp_path / "bin"
    _fake_python(binary_dir, version)
    sentinel = tmp_path / "staging-started"
    script = (
        _helper_selection_block()
        + 'printf "%s\\n" "$BUILD_PYTHON" > "$SELECTED_HELPER_PATH"\n'
        + ': > "$STAGING_SENTINEL"\n'
    )
    return subprocess.run(
        ["/bin/bash", "-eu", "-c", script],
        env={
            "PATH": str(binary_dir),
            "SELECTED_HELPER_PATH": str(tmp_path / "selected-helper"),
            "STAGING_SENTINEL": str(sentinel),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_deb_builder_uses_ci_selected_python_31214_before_staging(tmp_path: Path) -> None:
    result = _run_selection(tmp_path, "3.12.14")
    helper = tmp_path / "bin/python3"

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "selected-helper").read_text(encoding="utf-8").strip() == str(helper)
    assert (tmp_path / "staging-started").is_file()


@pytest.mark.parametrize("version", ["3.10.12", "3.12.13", "3.13.0"])
def test_deb_builder_rejects_wrong_helper_before_staging(tmp_path: Path, version: str) -> None:
    result = _run_selection(tmp_path, version)

    assert result.returncode == 2
    assert "build helpers require the selected Python 3.12.14" in result.stderr
    assert not (tmp_path / "staging-started").exists()
    assert not (tmp_path / "selected-helper").exists()


def test_all_host_helpers_use_selected_python_and_deployed_runtime_stays_locked() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'BUILD_PYTHON="$(command -v python3' in script
    assert "/usr/bin/python3" not in script
    assert '"${BUILD_PYTHON}" "${SCRIPT_DIR}/python_runtime.py"' in script
    assert '"${BUILD_PYTHON}" "${SCRIPT_DIR}/verified_service_artifacts.py"' in script
    assert 'RUNTIME_PYTHON="${STAGE_DIR}/opt/cyrene/python/3.12.14/bin/python3.12"' in script
