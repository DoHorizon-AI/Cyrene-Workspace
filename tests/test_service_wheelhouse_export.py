"""Regression tests for native target export and pip wheel tag selection."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cyrene_prepare_service_wheelhouse_export_test",
    WORKSPACE_ROOT / "packaging" / "prepare_service_wheelhouse.py",
)
assert SPEC is not None and SPEC.loader is not None
prepare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prepare
SPEC.loader.exec_module(prepare)


def test_uv_export_uses_supported_fixed_argv_without_cross_target_flag() -> None:
    output = Path("/tmp/wheelhouse/.exports/service.requirements.txt")
    project = Path("/tmp/product")

    arguments = prepare._uv_export_arguments("/tools/uv", "/tools/python3.12", output, project)

    assert arguments == [
        "/tools/uv",
        "export",
        "--locked",
        "--no-dev",
        "--no-default-groups",
        "--no-emit-project",
        "--no-emit-local",
        "--no-annotate",
        "--no-header",
        "--format",
        "requirements.txt",
        "--python",
        "/tools/python3.12",
        "--output-file",
        str(output),
        "--directory",
        str(project),
    ]
    assert "--python-platform" not in arguments


@pytest.mark.parametrize(
    ("profile_id", "ubuntu_version", "glibc", "target_floor"),
    [
        ("linux-ubuntu-22.04-x86_64-python-3.12", "22.04", "2.35", "manylinux_2_35_x86_64"),
        ("linux-ubuntu-24.04-x86_64-python-3.12", "24.04", "2.39", "manylinux_2_39_x86_64"),
    ],
)
def test_export_host_gate_and_locked_wheel_constraints_remain_enforced(
    monkeypatch: pytest.MonkeyPatch,
    profile_id: str,
    ubuntu_version: str,
    glibc: str,
    target_floor: str,
) -> None:
    lock = json.loads((WORKSPACE_ROOT / "release-lock.json").read_text(encoding="utf-8"))
    profile = prepare._native_python_profile(lock, profile_id)

    assert profile["wheelResolver"]["version"] == "0.12.21"
    assert profile["wheelResolver"]["arguments"] == [
        "--python-platform",
        "x86_64-unknown-linux-gnu",
    ]
    assert profile["wheelResolver"]["allowedWheelTags"]["pep600"]["maxGlibc"] == glibc
    prepare._validate_python_input(profile, WORKSPACE_ROOT)

    monkeypatch.setattr(
        prepare.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": ubuntu_version},
    )
    monkeypatch.setattr(prepare.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(prepare.platform, "libc_ver", lambda: ("glibc", glibc))

    assert prepare._native_target(profile) == "amd64"
    assert prepare._wheel_tag_is_compatible(f"cp312-cp312-{target_floor}", profile)
    assert not prepare._wheel_tag_is_compatible("cp312-cp312-manylinux_2_40_x86_64", profile)

    monkeypatch.setattr(
        prepare.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "22.04" if ubuntu_version == "24.04" else "24.04"},
    )
    monkeypatch.setattr(
        prepare.platform,
        "libc_ver",
        lambda: ("glibc", "2.35" if ubuntu_version == "24.04" else "2.39"),
    )
    with pytest.raises(prepare.ProducerError, match="does not match the build host"):
        prepare._native_target(profile)


@pytest.mark.parametrize(
    ("profile_id", "first_tag", "last_pep600_tag", "legacy_tags"),
    [
        (
            "linux-ubuntu-22.04-x86_64-python-3.12",
            "manylinux_2_35_x86_64",
            "manylinux_2_17_x86_64",
            ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"],
        ),
        (
            "linux-ubuntu-24.04-x86_64-python-3.12",
            "manylinux_2_39_x86_64",
            "manylinux_2_17_x86_64",
            ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"],
        ),
    ],
)
def test_pip_download_requests_all_and_only_compatible_x86_64_glibc_tags(
    profile_id: str,
    first_tag: str,
    last_pep600_tag: str,
    legacy_tags: list[str],
) -> None:
    lock = json.loads((WORKSPACE_ROOT / "release-lock.json").read_text(encoding="utf-8"))
    profile = prepare._native_python_profile(lock, profile_id)
    tags = prepare._pip_platform_tags(profile)
    arguments = prepare._pip_download_arguments(
        "/tools/python3.12",
        profile,
        Path("/wheelhouse/source"),
        Path("/wheelhouse/stage"),
        Path("/wheelhouse/requirements.txt"),
    )

    assert tags[0] == first_tag
    assert tags[-4] == last_pep600_tag
    assert tags[-3:] == legacy_tags
    assert (
        tags
        == [
            f"manylinux_2_{minor}_x86_64"
            for minor in range(int(profile["abi"].removeprefix("glibc-2.")), 16, -1)
        ]
        + legacy_tags
    )
    assert arguments.count("--platform") == len(tags)
    assert [
        arguments[index + 1] for index, value in enumerate(arguments) if value == "--platform"
    ] == tags
    assert arguments[arguments.index("--implementation") + 1] == "cp"
    assert arguments[arguments.index("--python-version") + 1] == "3.12"
    assert arguments[arguments.index("--abi") + 1] == "cp312"
    assert "--require-hashes" in arguments
    assert "--only-binary=:all:" in arguments
    assert "manylinux_2_40_x86_64" not in tags
    assert not any(tag.endswith(("aarch64", "i686")) for tag in tags)


@pytest.mark.parametrize(
    ("architecture", "max_glibc"),
    [("aarch64", "2.39"), ("x86_64", "3.1"), ("x86_64", "2.16")],
)
def test_pip_platform_tags_reject_unsupported_architecture_or_glibc(
    architecture: str,
    max_glibc: str,
) -> None:
    profile = {
        "wheelResolver": {
            "allowedWheelTags": {"pep600": {"architecture": architecture, "maxGlibc": max_glibc}}
        }
    }

    with pytest.raises(prepare.ProducerError, match="unsupported manylinux wheel constraints"):
        prepare._pip_platform_tags(profile)
