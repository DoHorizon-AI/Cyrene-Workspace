#!/usr/bin/env python3
"""
Administrator packet assembly for the signed, stage-only native closeout.

This controller-side tool accepts explicit immutable inputs, verifies the
official release through the exact bundled Workspace source, then writes a
reviewable packet and a hash-checking, root-only bootstrap launcher.
模块职责：验证精确签名输入，并生成可离线复核的一次性管理员数据包。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TOOLS_LOCK = HERE / "operator-tools.lock.json"
WORKSPACE_REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
ALLOWED_REFS = frozenset({"refs/heads/develop", "refs/heads/main", "refs/heads/release"})
SUPPORTED_UBUNTU = {"22.04", "24.04"}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
SAFE_OPERATOR_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
BOOTSTRAP_INPUTS = (
    ("index", "index.json"),
    ("index-attestation", "index.attestation.jsonl"),
    ("manifest", "manifest.json"),
    ("artifact", "artifact.tar.zst"),
    ("artifact-attestation", "artifact.attestation.jsonl"),
)
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)


class AdminPacketError(ValueError):
    """One explicit packet input, signature, or integrity check failed."""


def _sha256(path: Path) -> str:
    """Hash a regular file using bounded memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_lock(path: Path = TOOLS_LOCK) -> dict[str, Any]:
    """Read the fixed operator tool lock and reject incomplete pins."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdminPacketError(f"cannot read operator tool lock: {error}") from error
    if not isinstance(value, dict):
        raise AdminPacketError("operator tool lock must be a JSON object")
    gh = value.get("githubCli")
    if (
        value.get("schemaVersion") != 1
        or not isinstance(gh, dict)
        or gh.get("version") != "2.97.0"
        or gh.get("repository") != "cli/cli"
        or gh.get("tag") != "v2.97.0"
        or not SHA256_RE.fullmatch(str(gh.get("archiveSha256", "")))
        or not SHA256_RE.fullmatch(str(gh.get("checksumsSha256", "")))
        or not SHA256_RE.fullmatch(str(gh.get("binarySha256", "")))
        or not isinstance(gh.get("archiveSizeBytes"), int)
        or not isinstance(gh.get("checksumsSizeBytes"), int)
    ):
        raise AdminPacketError("operator tool lock is incomplete or unsupported")
    for field in ("sourceUrl", "checksumsSourceUrl"):
        parsed = urllib.parse.urlsplit(str(gh.get(field, "")))
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise AdminPacketError(f"operator tool lock has an untrusted {field}")
    return value


def _require_regular(path: Path, label: str) -> Path:
    """Reject symlinks, non-files, and paths that cannot be resolved."""

    if path.is_symlink() or not path.is_file():
        raise AdminPacketError(f"{label} must be a regular non-symlink file: {path}")
    resolved = path.resolve(strict=True)
    if resolved.stat().st_size == 0:
        raise AdminPacketError(f"{label} must not be empty: {path}")
    return resolved


def _safe_release_files(directory: Path) -> list[Path]:
    """Return only direct release assets and reject links or nested paths."""

    if directory.is_symlink() or not directory.is_dir():
        raise AdminPacketError("release directory must be a non-symlink directory")
    files: list[Path] = []
    for item in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not SAFE_ASSET_NAME_RE.fullmatch(item.name):
            raise AdminPacketError(f"release asset has an unsafe name: {item.name!r}")
        files.append(_require_regular(item, "release asset"))
    if not files:
        raise AdminPacketError("release directory is empty")
    return files


def _download(url: str, destination: Path, expected_sha256: str, expected_size: int) -> None:
    """Fetch one locked official asset and validate its final host and bytes."""

    request = urllib.request.Request(url, headers={"User-Agent": "cyrene-admin-packet/1"})
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=90) as response, destination.open("xb") as out:
            host = urllib.parse.urlsplit(response.geturl()).hostname
            if host not in ALLOWED_DOWNLOAD_HOSTS:
                raise AdminPacketError(f"official asset redirected to an untrusted host: {host}")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > expected_size:
                    raise AdminPacketError("official asset exceeds its locked size")
                digest.update(chunk)
                out.write(chunk)
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        destination.unlink(missing_ok=True)
        raise AdminPacketError(f"cannot download locked operator tool: {error}") from error
    if size != expected_size or digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        raise AdminPacketError(
            "downloaded operator tool does not match its locked size and SHA-256"
        )


def _locked_github_cli(destination: Path, lock: Mapping[str, Any]) -> Path:
    """Download official GitHub CLI assets and extract only the pinned executable."""

    gh = lock["githubCli"]
    scratch = destination.parent / "gh-downloads"
    archive_path = scratch / gh["assetName"]
    checksums_path = scratch / gh["checksumsAssetName"]
    _download(
        gh["checksumsSourceUrl"],
        checksums_path,
        gh["checksumsSha256"],
        gh["checksumsSizeBytes"],
    )
    _download(gh["sourceUrl"], archive_path, gh["archiveSha256"], gh["archiveSizeBytes"])
    checksum_lines = checksums_path.read_text(encoding="ascii").splitlines()
    expected_line = f"{gh['archiveSha256']}  {gh['assetName']}"
    if expected_line not in checksum_lines:
        raise AdminPacketError(
            "official GitHub CLI checksum asset does not list the pinned archive"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            candidates = [
                member for member in archive.getmembers() if member.name == gh["binaryPath"]
            ]
            if len(candidates) != 1 or not candidates[0].isfile():
                raise AdminPacketError(
                    "locked GitHub CLI archive lacks its unique regular executable"
                )
            source = archive.extractfile(candidates[0])
            if source is None:
                raise AdminPacketError("locked GitHub CLI executable cannot be read")
            with source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)
    except (OSError, tarfile.TarError) as error:
        destination.unlink(missing_ok=True)
        raise AdminPacketError(f"cannot inspect locked GitHub CLI archive: {error}") from error
    if _sha256(destination) != gh["binarySha256"]:
        destination.unlink(missing_ok=True)
        raise AdminPacketError(
            "extracted GitHub CLI executable differs from its pinned inner SHA-256"
        )
    destination.chmod(0o755)
    result = subprocess.run(
        [str(destination), "--version"], capture_output=True, check=False, text=True, timeout=30
    )
    if result.returncode != 0 or not result.stdout.startswith("gh version 2.97.0"):
        raise AdminPacketError("pinned GitHub CLI executable did not report version 2.97.0")
    return destination


def _run_git(arguments: list[str], *, cwd: Path | None = None) -> str:
    """Run a fixed Git argv and return its bounded textual result."""

    try:
        result = subprocess.run(
            arguments, cwd=cwd, capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdminPacketError(f"Git could not complete {Path(arguments[0]).name}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:400]
        raise AdminPacketError(f"Git failed: {detail}")
    return result.stdout.strip()


def _checkout_exact_bundle(bundle: Path, destination: Path, ref: str, commit: str) -> Path:
    """Clone a supplied bundle without mutating its source and require its exact ref."""

    _require_regular(bundle, "Workspace Git bundle")
    if ref not in ALLOWED_REFS or not COMMIT_RE.fullmatch(commit):
        raise AdminPacketError(
            "an allowed exact Workspace ref and full lowercase commit are required"
        )
    advertised_commits = []
    for line in _run_git(["git", "bundle", "list-heads", str(bundle)]).splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == ref:
            advertised_commits.append(fields[0])
    if advertised_commits != [commit]:
        raise AdminPacketError("source bundle ref does not resolve to the supplied full commit")
    _run_git(["git", "clone", "--no-checkout", str(bundle), str(destination)])
    _run_git(["git", "-C", str(destination), "cat-file", "-e", f"{commit}^{{commit}}"])
    _run_git(
        [
            "git",
            "-C",
            str(destination),
            "fetch",
            "--no-tags",
            str(bundle),
            f"{ref}:refs/cyrene-packet/selected-source",
        ]
    )
    actual = _run_git(
        ["git", "-C", str(destination), "rev-parse", "refs/cyrene-packet/selected-source^{commit}"]
    )
    if actual != commit:
        raise AdminPacketError("source bundle ref does not resolve to the supplied full commit")
    _run_git(["git", "-C", str(destination), "checkout", "--detach", commit])
    for required in (
        "tooling/acceptance/native-components-v2/native_acceptance.py",
        "tooling/acceptance/native-components-v2/admin_initialize.py",
        "scripts/native_installer_release.py",
        "packaging/python_runtime.py",
        "packaging/python-runtime.lock.json",
        "release-lock.json",
    ):
        _run_git(["git", "-C", str(destination), "cat-file", "-e", f"{commit}:{required}"])
    return destination


def _official_verifier(checkout: Path, gh: Path) -> Callable[..., dict[str, Any]]:
    """Load the verifier from the exact checked-out source and pin its GH executable."""

    module_path = checkout / "tooling/acceptance/native-components-v2/native_acceptance.py"
    spec = importlib.util.spec_from_file_location("cyrene_packet_native_acceptance", module_path)
    if spec is None or spec.loader is None:
        raise AdminPacketError("exact source bundle does not contain a loadable release verifier")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    os.environ["GH_EXECUTABLE"] = str(gh)
    return module.verify_native_release_for_host


def _validate_proof(
    proof: Mapping[str, Any],
    *,
    ref: str,
    commit: str,
    ubuntu_version: str,
    channel: str,
) -> None:
    """Require the official verifier receipt to identify the requested signed release."""

    expected_target = f"linux-ubuntu-{ubuntu_version}-x86_64-python-3.12"
    source = proof.get("source")
    target = proof.get("target")
    verifier = proof.get("verifier")
    if (
        proof.get("repository") != WORKSPACE_REPOSITORY
        or proof.get("channel") != channel
        or not isinstance(source, Mapping)
        or source.get("ref") != ref
        or source.get("commit") != commit
        or not isinstance(target, Mapping)
        or target.get("targetId") != expected_target
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(target.get("debSha256", "")))
        or not isinstance(verifier, Mapping)
        or verifier.get("attestationsVerified") is not True
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(verifier.get("sha256", "")))
    ):
        raise AdminPacketError(
            "official verifier did not return the exact fully attested release proof"
        )


def _copy_asset(source: Path, target: Path) -> dict[str, Any]:
    """Copy one already-validated regular input and return its immutable commitment."""

    source = _require_regular(source, "packet input")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, target.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
    asset_root = next((parent for parent in target.parents if parent.name == "assets"), None)
    if asset_root is None:
        raise AdminPacketError("packet asset target is outside its assets directory")
    return {
        "path": target.relative_to(asset_root.parent).as_posix(),
        "sha256": _sha256(target),
        "sizeBytes": target.stat().st_size,
    }


def _launcher_script(
    *,
    commitments: Mapping[str, Mapping[str, Any]],
    ref: str,
    commit: str,
    ubuntu_version: str,
    channel: str,
    deb_asset_name: str,
    deb_sha256: str,
    start_broker: bool,
    operator_user: str | None,
) -> str:
    """Render a fixed launcher with literal asset hashes and no caller-supplied commands."""

    lines = [
        "#!/bin/sh",
        "set -eu",
        "PATH=/usr/bin:/bin; export PATH",
        "unset PYTHONHOME PYTHONPATH PYTHONUSERBASE PYTHONSTARTUP PYTHONINSPECT LD_LIBRARY_PATH LD_PRELOAD LD_AUDIT GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_CONFIG GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL GIT_EXEC_PATH GIT_TEMPLATE_DIR GIT_SSH GIT_SSH_COMMAND GIT_ASKPASS SSH_ASKPASS GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GH_CONFIG_DIR GH_HOST GITHUB_HOST GITHUB_API_URL GH_PAGER GH_PROMPT_DISABLED",
        'packet_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)',
        "[ ! -L \"$0\" ] || { echo 'REFUSED: launcher is a symlink' >&2; exit 2; }",
        'for directory in assets assets/tools assets/release assets/bootstrap; do [ ! -L "$packet_dir/$directory" ] || { echo "REFUSED: linked packet directory: $directory" >&2; exit 2; }; done',
        "check_path() {",
        "  file=$1; expected=$2",
        '  [ -f "$file" ] && [ ! -L "$file" ] || { echo "REFUSED: missing or linked packet asset: $file" >&2; exit 2; }',
        "  actual=$(sha256sum -- \"$file\" | cut -d ' ' -f 1)",
        '  [ "$actual" = "$expected" ] || { echo "REFUSED: packet asset digest mismatch: $file" >&2; exit 2; }',
        "}",
        "check_asset() {",
        '  check_path "$packet_dir/$1" "$2"',
        "}",
    ]
    for relative, record in sorted(commitments.items()):
        lines.append(f"check_asset '{relative}' '{record['sha256']}'")
    lines.extend(
        [
            "[ \"$(id -u)\" -eq 0 ] || { echo 'Run this reviewed packet launcher as root' >&2; exit 2; }",
            "umask 077",
            "stage=$(mktemp -d /var/tmp/cyrene-admin-packet.XXXXXX)",
            '[ "$(stat -c %u "$stage")" = 0 ] && [ "$(stat -c %a "$stage")" = 700 ] || { echo \'REFUSED: unsafe private stage\' >&2; exit 2; }',
            "case \"$stage\" in /var/tmp/cyrene-admin-packet.*) ;; *) echo 'REFUSED: unexpected temporary stage path' >&2; exit 2 ;; esac",
            'cleanup() { rm -rf -- "$stage"; }',
            "trap cleanup EXIT",
            "trap 'exit 129' HUP",
            "trap 'exit 130' INT",
            "trap 'exit 143' TERM",
            'mkdir -m 700 "$stage/tools" "$stage/release" "$stage/bootstrap" "$stage/gh-config" "$stage/git-template"',
            'cp -- "$packet_dir/assets/tools/gh" "$stage/tools/gh"',
            'cp -- "$packet_dir/assets/workspace.bundle" "$stage/workspace.bundle"',
        ]
    )
    for relative in sorted(commitments):
        if relative.startswith("assets/release/"):
            filename = relative.removeprefix("assets/release/")
            lines.append(f'cp -- "$packet_dir/{relative}" "$stage/release/{filename}"')
    for _name, filename in BOOTSTRAP_INPUTS:
        lines.append(
            f'cp -- "$packet_dir/assets/bootstrap/{filename}" "$stage/bootstrap/{filename}"'
        )
    for relative, record in sorted(commitments.items()):
        if relative == "assets/tools/gh":
            staged_path = "$stage/tools/gh"
        elif relative == "assets/workspace.bundle":
            staged_path = "$stage/workspace.bundle"
        elif relative.startswith("assets/release/"):
            staged_path = "$stage/release/" + relative.removeprefix("assets/release/")
        elif relative.startswith("assets/bootstrap/"):
            staged_path = "$stage/bootstrap/" + relative.removeprefix("assets/bootstrap/")
        else:
            continue
        lines.append(f'check_path "{staged_path}" "{record["sha256"]}"')
    lines.extend(
        [
            'chmod 700 "$stage/tools/gh"',
            'PATH="$stage/tools:/usr/local/sbin:/usr/sbin:/sbin:/usr/bin:/bin"; export PATH',
            'GH_EXECUTABLE="$stage/tools/gh"; export GH_EXECUTABLE',
            'GH_CONFIG_DIR="$stage/gh-config"; export GH_CONFIG_DIR',
            "GIT_CONFIG_GLOBAL=/dev/null; export GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM=1; export GIT_CONFIG_NOSYSTEM",
            'CYRENE_PACKET_WORKSPACE="$stage/workspace"; export CYRENE_PACKET_WORKSPACE',
            f"SOURCE_REF='{ref}'",
            f"SOURCE_COMMIT='{commit}'",
            f"UBUNTU_VERSION='{ubuntu_version}'",
            f"CHANNEL='{channel}'",
            f"DEB_ASSET_NAME='{deb_asset_name}'",
            f"DEB_SHA256='{deb_sha256.removeprefix('sha256:')}'",
            '[ "$(sha256sum -- "$stage/release/$DEB_ASSET_NAME" | cut -d \' \' -f 1)" = "$DEB_SHA256" ] || { echo \'REFUSED: controller-verified DEB digest changed\' >&2; exit 2; }',
            'mkdir -m 700 "$stage/deb-extract"',
            'dpkg-deb --extract "$stage/release/$DEB_ASSET_NAME" "$stage/deb-extract"',
            'private_root="$stage/deb-extract/opt/cyrene/python/3.12.14"',
            'private_python="$private_root/bin/python3.12"',
            'for directory in "$stage/deb-extract/opt" "$stage/deb-extract/opt/cyrene" "$stage/deb-extract/opt/cyrene/python" "$private_root" "$private_root/bin"; do [ -d "$directory" ] && [ ! -L "$directory" ] && [ "$(stat -c %u "$directory")" = 0 ] || { echo "REFUSED: unsafe extracted private Python path: $directory" >&2; exit 2; }; done',
            '[ -d "$private_root" ] && [ ! -L "$private_root" ] || { echo \'REFUSED: locked private Python tree is missing or linked\' >&2; exit 2; }',
            '[ -f "$private_python" ] && [ ! -L "$private_python" ] && [ -x "$private_python" ] || { echo \'REFUSED: locked private Python executable is missing or linked\' >&2; exit 2; }',
            '[ "$(stat -c %u "$private_root")" = 0 ] && [ "$(stat -c %u "$private_python")" = 0 ] || { echo \'REFUSED: extracted private Python is not root-owned\' >&2; exit 2; }',
            'unowned=$(find "$private_root" -xdev ! -uid 0 -print -quit)',
            '[ -z "$unowned" ] || { echo "REFUSED: extracted private Python contains non-root-owned path: $unowned" >&2; exit 2; }',
            'python_version=$(env -u PYTHONPATH -u PYTHONHOME -u LD_LIBRARY_PATH PYTHONNOUSERSITE=1 "$private_python" --version 2>&1)',
            '[ "$python_version" = "Python 3.12.14" ] || { echo "REFUSED: unexpected private Python version: $python_version" >&2; exit 2; }',
            'env -u PYTHONPATH -u PYTHONHOME -u LD_LIBRARY_PATH PYTHONNOUSERSITE=1 "$private_python" -c \'import importlib.metadata as m, jsonschema, sys; assert sys.version_info[:3] == (3, 12, 14); assert m.version("jsonschema") == "4.26.0"; print("temporary verifier runtime: CPython 3.12.14, jsonschema 4.26.0")\'',
            'git clone --template="$stage/git-template" --no-checkout "$stage/workspace.bundle" "$stage/workspace"',
            'git -C "$stage/workspace" cat-file -e "$SOURCE_COMMIT^{commit}"',
            'git -C "$stage/workspace" fetch --no-tags "$stage/workspace.bundle" "$SOURCE_REF:refs/cyrene-packet/selected-source"',
            '[ "$(git -C "$stage/workspace" rev-parse refs/cyrene-packet/selected-source^{commit})" = "$SOURCE_COMMIT" ] || { echo \'REFUSED: source ref and commit differ\' >&2; exit 2; }',
            'git -C "$stage/workspace" checkout --detach "$SOURCE_COMMIT"',
        ]
    )
    # Controller proof authorizes extraction; the host verifier rechecks signatures before initializer installation.
    lines.extend(
        [
            'PYTHONNOUSERSITE=1 PYTHONPATH="$stage/workspace/tooling/acceptance/native-components-v2" "$private_python" - "$stage/release" "$SOURCE_REF" "$SOURCE_COMMIT" "$UBUNTU_VERSION" > "$stage/release-proof.json" <<\'PY\'',
            "import importlib.util, json, os, pathlib, sys",
            "workspace = pathlib.Path(os.environ['CYRENE_PACKET_WORKSPACE'])",
            "path = workspace / 'tooling/acceptance/native-components-v2/native_acceptance.py'",
            "spec = importlib.util.spec_from_file_location('native_acceptance_packet', path)",
            "module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)",
            "proof = module.verify_native_release_for_host(pathlib.Path(sys.argv[1]), expected_source_ref=sys.argv[2], expected_source_commit=sys.argv[3], ubuntu_version=sys.argv[4])",
            "if proof.get('verifier', {}).get('attestationsVerified') is not True: raise SystemExit('official attestations were not verified')",
            "print(json.dumps(proof, sort_keys=True))",
            "PY",
            'env -u PYTHONHOME -u LD_LIBRARY_PATH PYTHONNOUSERSITE=1 PYTHONPATH="$stage/workspace/tooling/acceptance/native-components-v2" "$private_python" "$stage/workspace/tooling/acceptance/native-components-v2/admin_initialize.py" --release-directory "$stage/release" --expected-source-ref "$SOURCE_REF" --expected-source-commit "$SOURCE_COMMIT" --index "$stage/bootstrap/index.json" --index-attestation "$stage/bootstrap/index.attestation.jsonl" --manifest "$stage/bootstrap/manifest.json" --artifact "$stage/bootstrap/artifact.tar.zst" --artifact-attestation "$stage/bootstrap/artifact.attestation.jsonl" --github-cli "$stage/tools/gh" --channel "$CHANNEL"',
        ]
    )
    if start_broker:
        lines[-1] += " --start-broker"
    if operator_user is not None:
        lines[-1] += f" --operator-user {shlex.quote(operator_user)}"
    return "\n".join(lines) + "\n"


def _build_packet(
    *,
    args: argparse.Namespace,
    verifier: Callable[..., dict[str, Any]],
    gh: Path,
    checkout: Path,
    stage: Path,
) -> Path:
    """Verify signed inputs, then assemble exact asset bytes and launcher."""

    proof = verifier(
        args.release_directory,
        expected_source_ref=args.source_ref,
        expected_source_commit=args.source_commit,
        ubuntu_version=args.ubuntu_version,
    )
    _validate_proof(
        proof,
        ref=args.source_ref,
        commit=args.source_commit,
        ubuntu_version=args.ubuntu_version,
        channel=args.channel,
    )
    if _run_git(["git", "-C", str(checkout), "rev-parse", "HEAD"]) != args.source_commit:
        raise AdminPacketError("source checkout moved away from the supplied immutable commit")
    release_files = _safe_release_files(args.release_directory)
    input_paths = {
        name: _require_regular(getattr(args, name.replace("-", "_")), name)
        for name, _ in BOOTSTRAP_INPUTS
    }
    bundle = _require_regular(args.source_bundle, "Workspace Git bundle")
    assets = stage / "assets"
    commitments: dict[str, dict[str, Any]] = {}
    commitments["assets/operator-tools.lock.json"] = _copy_asset(
        TOOLS_LOCK, assets / "operator-tools.lock.json"
    )
    commitments["assets/tools/gh"] = _copy_asset(gh, assets / "tools/gh")
    commitments["assets/workspace.bundle"] = _copy_asset(bundle, assets / "workspace.bundle")
    for source in release_files:
        relative = f"assets/release/{source.name}"
        commitments[relative] = _copy_asset(source, assets / "release" / source.name)
    for key, filename in BOOTSTRAP_INPUTS:
        relative = f"assets/bootstrap/{filename}"
        commitments[relative] = _copy_asset(input_paths[key], assets / "bootstrap" / filename)

    target_deb = proof["target"]["assetName"]
    if not SAFE_ASSET_NAME_RE.fullmatch(str(target_deb)):
        raise AdminPacketError("official verifier selected an unsafe DEB asset name")
    if f"assets/release/{target_deb}" not in commitments:
        raise AdminPacketError("official verifier selected a DEB absent from copied release bytes")
    metadata = {
        "schemaVersion": 1,
        "repository": WORKSPACE_REPOSITORY,
        "source": {"ref": args.source_ref, "commit": args.source_commit},
        "ubuntuVersion": args.ubuntu_version,
        "channel": args.channel,
        "startBrokerRequested": bool(args.start_broker),
        "operatorUser": args.operator_user,
        "releaseProof": proof,
        "assets": commitments,
        "temporaryVerifierRuntime": {
            "source": "exact controller-verified DEB bytes",
            "pythonPath": "/opt/cyrene/python/3.12.14/bin/python3.12 within private packet extraction",
            "version": "3.12.14",
            "installLocation": "root-owned packet staging only; no permanent /opt write before verification",
        },
        "persistentOperatorTool": {
            "name": "gh",
            "version": "2.97.0",
            "path": "/usr/libexec/cyrene-tools/gh",
            "sha256": _sha256(gh),
            "source": "assets/tools/gh verified against assets/operator-tools.lock.json",
        },
        "activationPolicy": {
            "products": "never-started",
            "coreServices": "never-started",
            "broker": (
                "start-requested-after-explicit-plan-confirmation"
                if args.start_broker
                else "not-started-by-packet-launcher"
            ),
            "unknownAuthority": "closed",
        },
        "createdAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    (stage / "admin-packet.manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (stage / "bootstrap.sh").write_text(
        _launcher_script(
            commitments=commitments,
            ref=args.source_ref,
            commit=args.source_commit,
            ubuntu_version=args.ubuntu_version,
            channel=args.channel,
            deb_asset_name=str(proof["target"]["assetName"]),
            deb_sha256=str(proof["target"]["debSha256"]),
            start_broker=bool(args.start_broker),
            operator_user=args.operator_user,
        ),
        encoding="utf-8",
    )
    (stage / "bootstrap.sh").chmod(0o755)
    return stage


def _assemble_packet(
    args: argparse.Namespace,
    *,
    verifier: Callable[..., dict[str, Any]] | None = None,
    lock: Mapping[str, Any] | None = None,
    github_cli_provider: Callable[[Path, Mapping[str, Any]], Path] | None = None,
) -> Path:
    """Assemble an atomic packet with dependency injection reserved for fixture tests.

    The public production entry point below supplies no overrides, so it always
    loads the verifier from the exact source bundle and performs real signature
    verification. Tests may inject deterministic fixture dependencies here.
    中文：正式命令始终调用真实官方签名校验；仅单元测试可注入固定结果。
    """

    if args.source_ref not in ALLOWED_REFS or not COMMIT_RE.fullmatch(args.source_commit):
        raise AdminPacketError(
            "an allowed exact Workspace ref and full lowercase commit are required"
        )
    if args.ubuntu_version not in SUPPORTED_UBUNTU:
        raise AdminPacketError("only Ubuntu 22.04 and 24.04 x86_64 packet targets are supported")
    if args.channel not in {"stable", "preview"}:
        raise AdminPacketError("channel must be stable or preview")
    if args.operator_user is not None and (
        not SAFE_OPERATOR_USER_RE.fullmatch(args.operator_user) or args.operator_user == "root"
    ):
        raise AdminPacketError("operator user must be a safe, non-root Linux username")
    bundle = _require_regular(args.source_bundle, "Workspace Git bundle")
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        raise AdminPacketError("packet output must be a new path")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink():
        raise AdminPacketError("packet output parent must not be a symlink")

    selected_lock = dict(lock or _read_lock())
    with tempfile.TemporaryDirectory(prefix="cyrene-admin-packet-", dir=output.parent) as temporary:
        work = Path(temporary)
        gh = (github_cli_provider or _locked_github_cli)(work / "tools/gh", selected_lock)
        checkout = _checkout_exact_bundle(
            bundle, work / "workspace", args.source_ref, args.source_commit
        )
        signature_verifier = verifier or _official_verifier(checkout, gh)
        packet_stage = work / "packet"
        packet_stage.mkdir(mode=0o700)
        _build_packet(
            args=args,
            verifier=signature_verifier,
            gh=gh,
            checkout=checkout,
            stage=packet_stage,
        )
        os.replace(packet_stage, output)
    return output


def assemble_packet(args: argparse.Namespace) -> Path:
    """Assemble a packet using only the real pinned verifier and tool downloader."""

    return _assemble_packet(args)


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit, no-floating-input packet assembly interface."""

    parser = argparse.ArgumentParser(
        description="Verify and assemble a signed, stage-only native administrator packet"
    )
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--ubuntu-version", choices=sorted(SUPPORTED_UBUNTU), required=True)
    parser.add_argument("--release-directory", type=Path, required=True)
    for name, _filename in BOOTSTRAP_INPUTS:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--channel", choices=("stable", "preview"), required=True)
    parser.add_argument(
        "--start-broker",
        action="store_true",
        help="Bind a broker-only start request after the initializer's explicit plan confirmation",
    )
    parser.add_argument(
        "--operator-user",
        help="Bind one safe, existing non-root Linux username to the administrator packet",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run production packet assembly with the real exact-source verifier."""

    try:
        packet = assemble_packet(build_parser().parse_args(argv))
    except (AdminPacketError, OSError, ValueError, KeyError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    print(f"Administrator packet assembled: {packet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
