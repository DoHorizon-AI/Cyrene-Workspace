"""Focused regression tests for the persistent updater CLI and DEB preflight."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
NATIVE_TOOLS = WORKSPACE_ROOT / "tooling/acceptance/native-components-v2"
sys.path.insert(0, str(NATIVE_TOOLS))
MODULE_PATH = NATIVE_TOOLS / "admin_initialize.py"
SPEC = importlib.util.spec_from_file_location("native_update_tool_dependencies_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
admin_initialize = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = admin_initialize
SPEC.loader.exec_module(admin_initialize)


def _result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_dependency_preflight_checks_installed_versions_and_alternatives(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if (
            arguments[:3] == ["/usr/bin/dpkg-deb", "--field", str(tmp_path / "cyrene.deb")]
            and arguments[-1] == "Depends"
        ):
            return _result("zstd, libc6 (>= 2.35), one-missing | systemd\n")
        if (
            arguments[:3] == ["/usr/bin/dpkg-deb", "--field", str(tmp_path / "cyrene.deb")]
            and arguments[-1] == "Architecture"
        ):
            return _result("amd64\n")
        if arguments[:2] == ["/usr/bin/dpkg", "--print-architecture"]:
            return _result("amd64\n")
        if arguments[0].endswith("dpkg-query"):
            package = arguments[-1]
            installed = {
                "zstd": [("zstd", "all", "ii ", "1.5.2")],
                "libc6": [
                    ("libc6:i386", "i386", "ii ", "2.35-0ubuntu3.99"),
                    ("libc6:amd64", "amd64", "ii ", "2.35-0ubuntu3"),
                ],
                "systemd": [("systemd", "amd64", "ii ", "255")],
            }
            rows = installed.get(package, [])
            output = "".join("\t".join(row) + "\n" for row in rows)
            return _result(output, 0 if rows else 1)
        if arguments[0].endswith("dpkg"):
            return _result(returncode=0)
        raise AssertionError(f"unexpected preflight command: {arguments}")

    admin_initialize._preflight_deb_dependencies(tmp_path / "cyrene.deb", runner=runner)
    assert any(call[1:] == ["--compare-versions", "2.35-0ubuntu3", ">=", "2.35"] for call in calls)
    assert not any(
        call[1:] == ["--compare-versions", "2.35-0ubuntu3.99", ">=", "2.35"] for call in calls
    )


def test_dependency_preflight_rejects_foreign_only_installed_architecture(
    tmp_path: Path,
) -> None:
    def runner(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[0].endswith("dpkg-deb"):
            return _result("zstd\n" if arguments[-1] == "Depends" else "amd64\n")
        if arguments[:2] == ["/usr/bin/dpkg", "--print-architecture"]:
            return _result("amd64\n")
        if arguments[0].endswith("dpkg-query"):
            return _result("zstd:i386\ti386\tii \t1.5.2\n")
        raise AssertionError(f"unexpected preflight command: {arguments}")

    with pytest.raises(admin_initialize.AdminInitializationError, match="zstd"):
        admin_initialize._preflight_deb_dependencies(tmp_path / "cyrene.deb", runner=runner)


def test_dependency_preflight_accepts_architecture_all_deb_and_dependency(tmp_path: Path) -> None:
    def runner(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[0].endswith("dpkg-deb"):
            return _result("zstd\n" if arguments[-1] == "Depends" else "all\n")
        if arguments[:2] == ["/usr/bin/dpkg", "--print-architecture"]:
            return _result("amd64\n")
        if arguments[0].endswith("dpkg-query"):
            return _result("zstd:all\tall\tii \t1.5.2\n")
        raise AssertionError(f"unexpected preflight command: {arguments}")

    admin_initialize._preflight_deb_dependencies(tmp_path / "cyrene.deb", runner=runner)


def test_dependency_preflight_rejects_deb_for_foreign_native_architecture(
    tmp_path: Path,
) -> None:
    def runner(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[0].endswith("dpkg-deb"):
            return _result("zstd\n" if arguments[-1] == "Depends" else "arm64\n")
        if arguments[:2] == ["/usr/bin/dpkg", "--print-architecture"]:
            return _result("amd64\n")
        raise AssertionError("foreign DEB architecture must fail before package queries")

    with pytest.raises(admin_initialize.AdminInitializationError, match="architecture"):
        admin_initialize._preflight_deb_dependencies(tmp_path / "cyrene.deb", runner=runner)


def test_dependency_preflight_fails_before_package_install_when_dependency_missing(
    tmp_path: Path,
) -> None:
    def runner(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[0].endswith("dpkg-deb"):
            return _result("zstd\n" if arguments[-1] == "Depends" else "amd64\n")
        if arguments[:2] == ["/usr/bin/dpkg", "--print-architecture"]:
            return _result("amd64\n")
        return _result(returncode=1)

    with pytest.raises(admin_initialize.AdminInitializationError, match="zstd"):
        admin_initialize._preflight_deb_dependencies(tmp_path / "cyrene.deb", runner=runner)


def test_dependency_preflight_rejects_unrecognized_control_syntax(tmp_path: Path) -> None:
    def runner(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[0].endswith("dpkg-deb"):
            return _result("zstd [amd64]\n" if arguments[-1] == "Depends" else "amd64\n")
        if arguments[:2] == ["/usr/bin/dpkg", "--print-architecture"]:
            return _result("amd64\n")
        raise AssertionError("unsupported dependency syntax must fail before package queries")

    with pytest.raises(admin_initialize.AdminInitializationError, match="Unsupported"):
        admin_initialize._preflight_deb_dependencies(tmp_path / "cyrene.deb", runner=runner)


def test_packet_cli_is_rejected_before_destination_writes_when_hash_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "gh"
    source.write_bytes(b"unverified tool")
    monkeypatch.setattr(admin_initialize, "_locked_github_cli_digest", lambda: "a" * 64)

    destination = tmp_path / "tools/gh"
    with pytest.raises(admin_initialize.AdminInitializationError, match="fixed binary SHA"):
        admin_initialize._persist_locked_github_cli(source, destination=destination)

    assert not destination.exists()


def test_component_update_helper_requires_pinned_tool_and_fixed_path() -> None:
    helper = (WORKSPACE_ROOT / "packaging/cyrene-component-update-helper").read_text()

    assert "PATH=/usr/libexec/cyrene-tools:/usr/sbin:/usr/bin:/sbin:/bin" in helper
    assert "141507c337e8b202ad398550c3b73d72f5af92e86f71665214538a81efd4c409" in helper
    assert "GH_CONFIG_DIR=/var/lib/cyrene/operator-tools/gh-config" in helper
    assert "exec /usr/bin/cyrene update --json" in helper
    subprocess.run(
        ["sh", "-n", str(WORKSPACE_ROOT / "packaging/cyrene-component-update-helper")], check=True
    )


def test_deb_declares_offline_artifact_decoder_dependency() -> None:
    build_script = (WORKSPACE_ROOT / "packaging/build-deb.sh").read_text()
    assert "Depends: " in build_script
    assert "policykit-1, acl, zstd" in build_script
