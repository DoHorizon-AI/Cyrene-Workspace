"""Fixture tests for the signed native administrator packet assembler."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
PACKET_PATH = WORKSPACE_ROOT / "tooling/acceptance/native-components-v2/admin_packet.py"
SPEC = importlib.util.spec_from_file_location("native_admin_packet_test_module", PACKET_PATH)
assert SPEC is not None and SPEC.loader is not None
admin_packet = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = admin_packet
SPEC.loader.exec_module(admin_packet)

TEST_COMMIT = "a" * 40
LOCK = {
    "schemaVersion": 1,
    "githubCli": {
        "version": "2.97.0",
        "repository": "cli/cli",
        "tag": "v2.97.0",
        "assetName": "gh_2.97.0_linux_amd64.tar.gz",
        "sourceUrl": "https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_linux_amd64.tar.gz",
        "archiveSha256": "a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112",
        "archiveSizeBytes": 14770812,
        "checksumsAssetName": "gh_2.97.0_checksums.txt",
        "checksumsSourceUrl": "https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_checksums.txt",
        "checksumsSha256": "61905c69ec8660f310814ec98395cdd0c2d07aabf024c597ec45813984a02334",
        "checksumsSizeBytes": 1950,
        "binaryPath": "gh_2.97.0_linux_amd64/bin/gh",
        "binarySha256": "141507c337e8b202ad398550c3b73d72f5af92e86f71665214538a81efd4c409",
    },
}


def _write(path: Path, content: bytes = b"fixture\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _source_bundle(root: Path) -> tuple[Path, str]:
    """Create a tiny exact-ref bundle with the required source tree paths."""

    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Packet Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "packet-test@example.invalid"],
        check=True,
    )
    for required in (
        "tooling/acceptance/native-components-v2/native_acceptance.py",
        "tooling/acceptance/native-components-v2/admin_initialize.py",
        "scripts/native_installer_release.py",
        "packaging/python_runtime.py",
        "packaging/python-runtime.lock.json",
        "release-lock.json",
    ):
        _write(repo / required)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture source"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    bundle = root / "workspace.bundle"
    subprocess.run(
        ["git", "-C", str(repo), "bundle", "create", str(bundle), "--all"],
        check=True,
        capture_output=True,
    )
    return bundle, commit


def _proof(ref: str, commit: str, ubuntu_version: str) -> dict[str, object]:
    return {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "channel": "preview",
        "source": {"ref": ref, "commit": commit},
        "target": {
            "targetId": f"linux-ubuntu-{ubuntu_version}-x86_64-python-3.12",
            "assetName": "cyrene-native-host_1.0_amd64.deb",
            "debSha256": "sha256:" + "b" * 64,
        },
        "verifier": {"sha256": "c" * 64, "attestationsVerified": True},
    }


def _args(root: Path, bundle: Path, commit: str) -> object:
    release = root / "release"
    _write(release / "cyrene-native-host_1.0_amd64.deb")
    for filename in (
        "native-installer-release-v1.json",
        "native-installer-source-receipt-v1.json",
        "SHA256SUMS",
    ):
        _write(release / filename)
    values: dict[str, object] = {
        "source_bundle": bundle,
        "source_ref": "refs/heads/main",
        "source_commit": commit,
        "ubuntu_version": "22.04",
        "release_directory": release,
        "channel": "preview",
        "start_broker": False,
        "output": root / "packet",
    }
    for name, _filename in admin_packet.BOOTSTRAP_INPUTS:
        values[name.replace("-", "_")] = _write(root / "inputs" / _filename)
    return type("PacketArgs", (), values)()


def _fake_gh_provider(root: Path):
    executable = _write(root / "fixture-gh", b"fixture gh executable")

    def provide(destination: Path, _lock: object) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(executable.read_bytes())
        return destination

    return provide


def test_assembler_creates_hash_bound_reviewable_stage_only_packet(tmp_path: Path) -> None:
    bundle, commit = _source_bundle(tmp_path)
    args = _args(tmp_path, bundle, commit)
    verifier_calls: list[tuple[Path, str, str, str]] = []

    def verify(
        directory: Path,
        *,
        expected_source_ref: str,
        expected_source_commit: str,
        ubuntu_version: str,
    ):
        verifier_calls.append(
            (directory, expected_source_ref, expected_source_commit, ubuntu_version)
        )
        return _proof(expected_source_ref, expected_source_commit, ubuntu_version)

    packet = admin_packet._assemble_packet(
        args,
        verifier=verify,
        lock=LOCK,
        github_cli_provider=_fake_gh_provider(tmp_path),
    )

    manifest = json.loads((packet / "admin-packet.manifest.json").read_text())
    launcher = (packet / "bootstrap.sh").read_text()
    assert len(verifier_calls) == 1
    assert manifest["source"] == {"ref": "refs/heads/main", "commit": commit}
    assert manifest["releaseProof"]["verifier"]["attestationsVerified"] is True
    assert manifest["startBrokerRequested"] is False
    assert manifest["activationPolicy"] == {
        "products": "never-started",
        "coreServices": "never-started",
        "broker": "not-started-by-packet-launcher",
        "unknownAuthority": "closed",
    }
    for relative, record in manifest["assets"].items():
        payload = (packet / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
        assert f"check_asset '{relative}' '{record['sha256']}'" in launcher
    assert launcher.index("check_asset 'assets/workspace.bundle'") < launcher.index(
        "stage=$(mktemp"
    )
    assert launcher.index("python_version=$(env") < launcher.index("verify_native_release_for_host")
    assert launcher.index('"$private_python" - "$stage/release"') < launcher.index(
        "verify_native_release_for_host"
    )
    assert 'find "$private_root" -xdev ! -uid 0' in launcher
    assert launcher.index('dpkg-deb --extract "$stage/release/$DEB_ASSET_NAME"') < launcher.index(
        "verify_native_release_for_host"
    )
    assert "DEB_SHA256='" + "b" * 64 + "'" in launcher
    assert "--start-broker" not in launcher
    assert "admin_initialize.py" in launcher
    assert "python_runtime.py" not in launcher
    assert "/usr/bin/python3" not in launcher
    assert launcher.index("verify_native_release_for_host") < launcher.index("admin_initialize.py")
    subprocess.run(["sh", "-n", str(packet / "bootstrap.sh")], check=True)


def test_packet_refuses_unattested_or_mismatched_verifier_receipt(tmp_path: Path) -> None:
    bundle, commit = _source_bundle(tmp_path)
    args = _args(tmp_path, bundle, commit)
    bad = _proof("refs/heads/main", commit, "22.04")
    bad["verifier"]["attestationsVerified"] = False  # type: ignore[index]

    with pytest.raises(admin_packet.AdminPacketError, match="official verifier"):
        admin_packet._assemble_packet(
            args,
            verifier=lambda *_args, **_kwargs: bad,
            lock=LOCK,
            github_cli_provider=_fake_gh_provider(tmp_path),
        )
    assert not args.output.exists()


def test_start_broker_is_an_explicit_packet_bound_choice(tmp_path: Path) -> None:
    bundle, commit = _source_bundle(tmp_path)
    args = _args(tmp_path, bundle, commit)
    args.start_broker = True
    args.output = tmp_path / "packet-with-broker-request"

    packet = admin_packet._assemble_packet(
        args,
        verifier=lambda _directory, *, expected_source_ref, expected_source_commit, ubuntu_version: (
            _proof(expected_source_ref, expected_source_commit, ubuntu_version)
        ),
        lock=LOCK,
        github_cli_provider=_fake_gh_provider(tmp_path),
    )

    manifest = json.loads((packet / "admin-packet.manifest.json").read_text())
    launcher = (packet / "bootstrap.sh").read_text()
    initializer_command = next(
        line for line in launcher.splitlines() if "admin_initialize.py" in line
    )
    assert manifest["startBrokerRequested"] is True
    assert (
        manifest["activationPolicy"]["broker"] == "start-requested-after-explicit-plan-confirmation"
    )
    assert initializer_command.endswith("--start-broker")
    assert "--start-core" not in launcher and "--start-product" not in launcher
    subprocess.run(["sh", "-n", str(packet / "bootstrap.sh")], check=True)


def test_packet_requires_source_ref_to_resolve_to_exact_commit(tmp_path: Path) -> None:
    bundle, commit = _source_bundle(tmp_path)
    args = _args(tmp_path, bundle, commit)
    args.source_commit = "d" * 40

    with pytest.raises(admin_packet.AdminPacketError, match="Git failed"):
        admin_packet._assemble_packet(
            args,
            verifier=lambda *_args, **_kwargs: {},
            lock=LOCK,
            github_cli_provider=_fake_gh_provider(tmp_path),
        )


def test_release_asset_symlink_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    target = _write(tmp_path / "outside.deb")
    (release / "linked.deb").symlink_to(target)

    with pytest.raises(admin_packet.AdminPacketError, match="non-symlink"):
        admin_packet._safe_release_files(release)


def test_operator_tool_lock_contains_verified_fixed_cli_asset_pins() -> None:
    lock = admin_packet._read_lock()
    gh = lock["githubCli"]
    assert gh["version"] == "2.97.0"
    assert gh["sourceUrl"].endswith("gh_2.97.0_linux_amd64.tar.gz")
    assert gh["archiveSha256"] == "a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112"
    assert gh["binarySha256"] == "141507c337e8b202ad398550c3b73d72f5af92e86f71665214538a81efd4c409"
