"""Guard package builder setup against precreating locked Python outputs."""

from __future__ import annotations

import json
import os
import re
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


def test_deb_package_version_survives_os_release_variable_import(tmp_path: Path) -> None:
    """Sourcing Ubuntu metadata must not replace the requested Debian package version."""

    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    control_start = script.index('cat <<EOF > "${STAGE_DIR}/DEBIAN/control"\n')
    control_end = script.index("\nEOF\n", control_start)
    control = script[control_start:control_end]
    version_match = re.search(r"^Version: \$\{([A-Z_]+)\}$", control, re.MULTILINE)
    assert version_match is not None
    version_variable = version_match.group(1)

    release_source_start = script.index("# shellcheck disable=SC1091\n")
    release_source_end = script.index("\nif [[", release_source_start)
    release_source = script[release_source_start:release_source_end].replace(
        ". /etc/os-release", '. "$OS_RELEASE_FIXTURE"'
    )
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\nVERSION="24.04.4 LTS (Noble Numbat)"\n',
        encoding="utf-8",
    )
    command = (
        f'{version_variable}="0.1.0-rc.1"\n'
        f"{release_source}\n"
        f'printf "%s\\n" "${{{version_variable}}}"\n'
    )
    result = subprocess.run(
        ["bash", "-eu", "-c", command],
        env={**os.environ, "OS_RELEASE_FIXTURE": str(os_release)},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "0.1.0-rc.1"
    assert 'OUTPUT_PACKAGE="${OUTPUT_DIR}/cyrene_${PACKAGE_VERSION}_${ARCH}.deb"' in script
