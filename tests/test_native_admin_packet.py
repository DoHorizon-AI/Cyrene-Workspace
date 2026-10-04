"""Fixture tests for the signed native administrator packet assembler."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shlex
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


def _source_bundle(
    root: Path, *, advertised_branch: str = "main", advertise_selected_branch_only: bool = False
) -> tuple[Path, str]:
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
    if advertised_branch != "main":
        subprocess.run(
            ["git", "-C", str(repo), "branch", advertised_branch],
            check=True,
            capture_output=True,
        )
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    bundle = root / "workspace.bundle"
    bundle_args = (
        ["git", "-C", str(repo), "bundle", "create", str(bundle), f"refs/heads/{advertised_branch}"]
        if advertise_selected_branch_only
        else ["git", "-C", str(repo), "bundle", "create", str(bundle), "--all"]
    )
    subprocess.run(bundle_args, check=True, capture_output=True)
    return bundle, commit


def _proof(ref: str, commit: str, ubuntu_version: str) -> dict[str, object]:
    channel = "preview" if ref == "refs/heads/develop" else "stable"
    return {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "releaseId": f"native-installer-{channel}-{commit}",
        "version": "0.1.0-rc.1",
        "channel": channel,
        "workflow": (
            "DoHorizon-AI/Cyrene-Workspace/.github/workflows/native-installer-release.yml"
        ),
        "run": {"id": 37213549672, "attempt": 1},
        "source": {
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "ref": ref,
            "commit": commit,
        },
        "target": {
            "targetId": f"linux-ubuntu-{ubuntu_version}-x86_64-python-3.12",
            "assetName": f"cyrene_0.1.0-rc.1_ubuntu-{ubuntu_version}_amd64.deb",
            "debSha256": "sha256:" + "b" * 64,
            "debSizeBytes": 123,
            "serviceArtifactsIndexPath": "/usr/share/cyrene/service-artifacts/index.json",
            "serviceArtifactsIndexSha256": "c" * 64,
            "maintainerScriptsSha256": {
                "postinst": "d" * 64,
                "prerm": "e" * 64,
                "postrm": "f" * 64,
            },
            "services": {},
            "checks": {
                "serviceActivation": "deferred",
                "brokerAction": "preserve-existing",
                "oldRuntimeAction": "preserve",
                "maintainerScriptsStaticScan": "passed",
                "verifiedServiceBytesPreserved": "passed",
                "freshBrokerUnavailable": "fail-closed",
                "upgradeState": "preserve-existing",
                "activeRuntimePointers": "preserve-existing",
                "pinnedPrivateRuntime": "passed",
            },
        },
        "manifest": {
            "path": "/release/native-installer-release-v1.json",
            "sha256": "sha256:" + "1" * 64,
        },
        "sourceReceipt": {
            "path": "/release/native-installer-source-receipt-v1.json",
            "sha256": "sha256:" + "2" * 64,
        },
        "verifier": {
            "path": "/workspace/scripts/native_installer_release.py",
            "sha256": "sha256:" + "c" * 64,
            "attestationsVerified": True,
        },
        "verifiedAtUtc": "2026-10-04T16:08:41+00:00",
    }


def _args(root: Path, bundle: Path, commit: str) -> object:
    release = root / "release"
    source_ref = "refs/heads/main"
    _write(release / "cyrene_0.1.0-rc.1_ubuntu-22.04_amd64.deb")
    for filename in (
        "native-installer-release-v1.json",
        "native-installer-source-receipt-v1.json",
        "SHA256SUMS",
    ):
        _write(release / filename)
    values: dict[str, object] = {
        "source_bundle": bundle,
        "source_ref": source_ref,
        "source_commit": commit,
        "ubuntu_version": "22.04",
        "release_directory": release,
        "channel": "preview" if source_ref == "refs/heads/develop" else "stable",
        "start_broker": False,
        "operator_user": None,
        "output": root / "packet",
    }
    for name, _filename in admin_packet.BOOTSTRAP_INPUTS:
        values[name.replace("-", "_")] = _write(root / "inputs" / _filename)
    return type("PacketArgs", (), values)()


def test_packet_proof_validator_accepts_official_prefixed_digest_shape() -> None:
    proof = _proof("refs/heads/develop", TEST_COMMIT, "24.04")

    admin_packet._validate_proof(
        proof,
        ref="refs/heads/develop",
        commit=TEST_COMMIT,
        ubuntu_version="24.04",
        channel="preview",
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda proof: proof.update(repository="DoHorizon-AI/Other"),
        lambda proof: proof.update(channel="stable"),
        lambda proof: proof["source"].update(ref="refs/heads/main"),
        lambda proof: proof["source"].update(commit="b" * 40),
        lambda proof: proof["target"].update(targetId="linux-ubuntu-22.04-x86_64-python-3.12"),
        lambda proof: proof["target"].update(debSha256="b" * 64),
        lambda proof: proof["verifier"].update(sha256="c" * 64),
        lambda proof: proof["verifier"].update(attestationsVerified=False),
    ],
)
def test_packet_proof_validator_rejects_mismatched_or_unattested_fields(mutate: object) -> None:
    proof = _proof("refs/heads/develop", TEST_COMMIT, "24.04")
    mutate(proof)

    with pytest.raises(admin_packet.AdminPacketError, match="exact fully attested"):
        admin_packet._validate_proof(
            proof,
            ref="refs/heads/develop",
            commit=TEST_COMMIT,
            ubuntu_version="24.04",
            channel="preview",
        )


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
    assert manifest["operatorUser"] is None
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
    copied_assets = {
        "assets/tools/gh": "$stage/tools/gh",
        "assets/workspace.bundle": "$stage/workspace.bundle",
        **{
            relative: "$stage/release/" + relative.removeprefix("assets/release/")
            for relative in manifest["assets"]
            if relative.startswith("assets/release/")
        },
        **{
            relative: "$stage/bootstrap/" + relative.removeprefix("assets/bootstrap/")
            for relative in manifest["assets"]
            if relative.startswith("assets/bootstrap/")
        },
    }
    first_execution = min(
        launcher.index("dpkg-deb --extract"),
        launcher.index("python_version=$(env"),
        launcher.index('git clone --template="$stage/git-template"'),
    )
    for relative, staged_path in copied_assets.items():
        digest = manifest["assets"][relative]["sha256"]
        copy_source = f'cp -- "$packet_dir/{relative}"'
        assert copy_source in launcher
        staged_guard = f'check_path "{staged_path}" "{digest}"'
        assert staged_guard in launcher
        assert launcher.index(copy_source) < launcher.index(staged_guard) < first_execution
    assert launcher.splitlines()[2] == "PATH=/usr/bin:/bin; export PATH"
    assert launcher.splitlines()[3].startswith("unset PYTHONHOME PYTHONPATH PYTHONUSERBASE")
    assert "LD_PRELOAD" in launcher.splitlines()[3] and "GIT_DIR" in launcher.splitlines()[3]
    guard_start = launcher.index("check_path() {")
    guard_end = launcher.index("\n}\ncheck_asset()", guard_start) + 2
    tampered_copy = _write(tmp_path / "tampered-gh-copy", b"changed after packet hash check\n")
    guard_result = subprocess.run(
        [
            "sh",
            "-c",
            launcher[guard_start:guard_end] + '\ncheck_path "$1" "$2"',
            "check-staged-copy",
            str(tampered_copy),
            manifest["assets"]["assets/tools/gh"]["sha256"],
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert guard_result.returncode == 2
    assert "digest mismatch" in guard_result.stderr
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


@pytest.mark.parametrize("ubuntu_version", ["22.04", "24.04"])
def test_generated_launcher_path_resolves_dpkg_maintainer_tools_without_user_path(
    tmp_path: Path, ubuntu_version: str
) -> None:
    """Resolve pinned and dpkg system tools without inheriting caller PATH entries."""

    stage = tmp_path / "stage"
    tools = {
        "gh": stage / "tools/gh",
        "local_helper": stage / "mock/usr/local/sbin/cyrene-local-admin-tool",
        "ldconfig": stage / "mock/usr/sbin/ldconfig",
        "start_stop_daemon": stage / "mock/sbin/start-stop-daemon",
    }
    user_bin = tmp_path / "user-bin"
    for name, path in tools.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\necho {name}\n", encoding="utf-8")
        path.chmod(0o755)
        inherited = user_bin / path.name
        inherited.parent.mkdir(parents=True, exist_ok=True)
        inherited.write_text("#!/bin/sh\necho inherited-user-tool\n", encoding="utf-8")
        inherited.chmod(0o755)

    launcher = admin_packet._launcher_script(
        commitments={},
        ref="refs/heads/develop",
        commit=TEST_COMMIT,
        ubuntu_version=ubuntu_version,
        channel="preview",
        deb_asset_name=f"cyrene_0.1.0-rc.1_ubuntu-{ubuntu_version}_amd64.deb",
        deb_sha256="b" * 64,
        start_broker=False,
        operator_user=None,
    )
    path_line = next(
        line for line in launcher.splitlines() if line.startswith('PATH="$stage/tools:')
    )
    assert (
        path_line
        == 'PATH="$stage/tools:/usr/local/sbin:/usr/sbin:/sbin:/usr/bin:/bin"; export PATH'
    )
    simulated_path_line = (
        path_line.replace("/usr/local/sbin", "@LOCAL_SBIN@")
        .replace("/usr/sbin", "@USR_SBIN@")
        .replace("/sbin", "@SBIN@")
        .replace("@LOCAL_SBIN@", "$stage/mock/usr/local/sbin")
        .replace("@USR_SBIN@", "$stage/mock/usr/sbin")
        .replace("@SBIN@", "$stage/mock/sbin")
    )
    script = "\n".join(
        [
            f"stage={shlex.quote(str(stage))}",
            simulated_path_line,
            "command -v gh",
            "command -v cyrene-local-admin-tool",
            "command -v ldconfig",
            "command -v start-stop-daemon",
        ]
    )
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        env={"PATH": str(user_bin)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(path) for path in tools.values()]
    assert "inherited-user-tool" not in result.stdout


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
    args.operator_user = "ruanyun"
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
    assert manifest["operatorUser"] == "ruanyun"
    assert initializer_command.endswith("--start-broker --operator-user ruanyun")
    assert "--start-core" not in launcher and "--start-product" not in launcher
    subprocess.run(["sh", "-n", str(packet / "bootstrap.sh")], check=True)


@pytest.mark.parametrize("operator_user", ["root", "0", "UPPER", "ruanyun;id", "user name"])
def test_packet_rejects_unsafe_or_root_operator_user(tmp_path: Path, operator_user: str) -> None:
    bundle, commit = _source_bundle(tmp_path)
    args = _args(tmp_path, bundle, commit)
    args.operator_user = operator_user

    with pytest.raises(admin_packet.AdminPacketError, match="safe, non-root"):
        admin_packet._assemble_packet(args)

    assert not args.output.exists()


def test_packet_requires_source_ref_to_resolve_to_exact_commit(tmp_path: Path) -> None:
    bundle, commit = _source_bundle(tmp_path)
    args = _args(tmp_path, bundle, commit)
    args.source_commit = "d" * 40

    with pytest.raises(admin_packet.AdminPacketError, match="source bundle ref"):
        admin_packet._assemble_packet(
            args,
            verifier=lambda *_args, **_kwargs: {},
            lock=LOCK,
            github_cli_provider=_fake_gh_provider(tmp_path),
        )


def test_checkout_materializes_advertised_develop_branch_when_bundle_head_is_absent(
    tmp_path: Path,
) -> None:
    bundle, commit = _source_bundle(
        tmp_path, advertised_branch="develop", advertise_selected_branch_only=True
    )

    checkout = admin_packet._checkout_exact_bundle(
        bundle, tmp_path / "controller-checkout", "refs/heads/develop", commit
    )

    assert admin_packet._run_git(["git", "-C", str(checkout), "rev-parse", "HEAD"]) == commit
    assert (
        admin_packet._run_git(
            [
                "git",
                "-C",
                str(checkout),
                "rev-parse",
                "refs/cyrene-packet/selected-source^{commit}",
            ]
        )
        == commit
    )


@pytest.mark.parametrize(
    ("ref", "commit", "message"),
    [
        ("refs/heads/main", "a" * 40, "source bundle ref"),
        ("refs/heads/unknown", "a" * 40, "allowed exact Workspace ref"),
        ("refs/heads/develop", "b" * 40, "source bundle ref"),
    ],
)
def test_checkout_rejects_unadvertised_ref_or_wrong_full_commit(
    tmp_path: Path, ref: str, commit: str, message: str
) -> None:
    bundle, actual_commit = _source_bundle(
        tmp_path, advertised_branch="develop", advertise_selected_branch_only=True
    )
    if commit == "a" * 40:
        commit = actual_commit

    with pytest.raises(admin_packet.AdminPacketError, match=message):
        admin_packet._checkout_exact_bundle(bundle, tmp_path / "bad-checkout", ref, commit)
    assert not (tmp_path / "bad-checkout").exists()


def _launcher_checkout_commands(script: str) -> str:
    """Extract only the generated launcher's non-privileged source checkout commands."""

    start = script.index("git clone --template=")
    end_line = 'git -C "$stage/workspace" checkout --detach "$SOURCE_COMMIT"'
    end = script.index(end_line, start) + len(end_line)
    return script[start:end]


@pytest.mark.parametrize(
    ("ref", "commit", "expected_status"),
    [
        ("refs/heads/develop", None, 0),
        ("refs/heads/main", None, 2),
        ("refs/heads/unknown", None, 2),
        ("refs/heads/develop", "b" * 40, 2),
    ],
)
def test_generated_launcher_materializes_and_checks_advertised_branch_before_checkout(
    tmp_path: Path, ref: str, commit: str | None, expected_status: int
) -> None:
    bundle, actual_commit = _source_bundle(
        tmp_path, advertised_branch="develop", advertise_selected_branch_only=True
    )
    stage = tmp_path / "launcher-stage"
    (stage / "git-template").mkdir(parents=True)
    (stage / "workspace.bundle").write_bytes(bundle.read_bytes())
    script = admin_packet._launcher_script(
        commitments={},
        ref="refs/heads/develop",
        commit=actual_commit,
        ubuntu_version="22.04",
        channel="preview",
        deb_asset_name="fixture.deb",
        deb_sha256="b" * 64,
        start_broker=False,
        operator_user=None,
    )
    selected_commit = commit or actual_commit

    result = subprocess.run(
        ["sh", "-c", _launcher_checkout_commands(script)],
        env={
            "PATH": "/usr/bin:/bin",
            "stage": str(stage),
            "SOURCE_REF": ref,
            "SOURCE_COMMIT": selected_commit,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_status, result.stderr
    workspace = stage / "workspace"
    if expected_status == 0:
        assert (
            admin_packet._run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"])
            == actual_commit
        )
    else:
        assert not (
            workspace / "tooling/acceptance/native-components-v2/native_acceptance.py"
        ).exists()


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
