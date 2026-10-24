"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 transfer_arm64.py                                                    │
│  Module: tooling.deploy.teams_cloud.transfer_arm64                      │
│  Role: Preflight a trusted VM and stream locked images directly to Docker.│
│                                                                          │
│  模块职责：预检可信云机并将已锁定镜像直接流式导入 Docker。               │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from build_arm64 import BuildError, check_local_lock

GIB = 1024**3
# Match preflight's nominal 4 GiB guest class; this is not an available-memory gate.
# 中文：允许固件和内核保留内存；部署仍须通过实际 MemAvailable 和 RSS 检查。
MIN_GUEST_MEMORY_MIB = 3584
SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
HOST_ALIAS_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


class TransferError(ValueError):
    """A trust, capacity, transfer, or remote read-back gate failed."""


def _private_output(path: Path) -> bool:
    """Require transfer evidence to stay under the umbrella artifact archive."""

    resolved = path.resolve()
    return ".artifacts" in resolved.parts and not any(
        (parent / ".git").exists() for parent in (resolved, *resolved.parents)
    )


def _ssh_base(target: str, user: str, host_key_alias: str, identity_file: Path) -> list[str]:
    """Build SSH arguments with strict host-key and identity selection."""

    if not SSH_TARGET_RE.fullmatch(target) or not HOST_ALIAS_RE.fullmatch(host_key_alias):
        raise TransferError("SSH target and HostKeyAlias must be explicit identifiers")
    if (
        identity_file.is_symlink()
        or not identity_file.is_file()
        or identity_file.stat().st_mode & 0o077
    ):
        raise TransferError("SSH identity file must be a protected regular file")
    return [
        "ssh",
        "-o",
        f"User={user}",
        "-o",
        f"HostKeyAlias={host_key_alias}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
        "-i",
        str(identity_file.resolve()),
        target,
    ]


def _remote(ssh: list[str], argv: list[str], *, capture: bool = True) -> str:
    """Run one fixed remote command through strict SSH without local shell use."""

    result = subprocess.run([*ssh, shlex.join(argv)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise TransferError("trusted-host read-back command failed")
    return result.stdout if capture else ""


def _remote_facts(ssh: list[str]) -> dict[str, Any]:
    """Confirm ARM64 Docker, two CPUs, the 4 GiB guest class, and root free space."""

    architecture = (
        _remote(ssh, ["sudo", "-n", "docker", "info", "--format", "{{.Architecture}}"])
        .strip()
        .lower()
    )
    if architecture not in {"aarch64", "arm64"}:
        raise TransferError(f"Docker daemon architecture is {architecture!r}, expected ARM64")
    processors = int(_remote(ssh, ["getconf", "_NPROCESSORS_ONLN"]).strip())
    memory_lines = (
        _remote(ssh, ["awk", "/MemTotal:/ {print $2}", "/proc/meminfo"]).strip().splitlines()
    )
    if len(memory_lines) != 1:
        raise TransferError("remote memory capacity could not be read")
    memory_mib = int(memory_lines[0]) // 1024
    df_lines = _remote(ssh, ["df", "-Pk", "/"]).strip().splitlines()
    if len(df_lines) < 2:
        raise TransferError("remote root filesystem capacity could not be read")
    free_kib = int(df_lines[-1].split()[3])
    if processors < 2 or memory_mib < MIN_GUEST_MEMORY_MIB:
        raise TransferError(
            f"remote host capacity is below the pinned profile ({processors} CPU, {memory_mib} MiB)"
        )
    return {
        "architecture": architecture,
        "processors": processors,
        "memoryMiB": memory_mib,
        "rootFreeBytes": free_kib * 1024,
    }


def _load_lock(path: Path) -> dict[str, Any]:
    """Check all locally built tags and collect the exact transfer set."""

    check_local_lock(path)
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransferError("image lock is unreadable") from error
    references: list[str] = []
    for row in lock.get("images", []):
        image = row.get("image")
        if not isinstance(image, str) or not IMAGE_REF_RE.fullmatch(image):
            raise TransferError("image lock contains an invalid image reference")
        references.append(image)
    for row in lock.get("externalImages", {}).values():
        image = row.get("localImage")
        if not isinstance(image, str) or not IMAGE_REF_RE.fullmatch(image):
            raise TransferError("external image lock contains an invalid local reference")
        references.append(image)
    if not references:
        raise TransferError("image lock contains no transfer artifacts")
    return lock


def _locked_size_bytes(lock: dict[str, Any]) -> int:
    """Return the conservative sum of exact image sizes from the build lock."""

    rows = [*lock.get("images", []), *lock.get("externalImages", {}).values()]
    sizes = [row.get("sizeBytes") for row in rows]
    if any(not isinstance(size, int) or size <= 0 for size in sizes):
        raise TransferError("image lock is missing measured local image sizes")
    return sum(sizes)


def transfer(
    lock_path: Path,
    receipt_path: Path,
    ssh: list[str],
    *,
    execute: bool,
    runtime_reserve_bytes: int,
) -> dict[str, Any]:
    """Stream a Docker save archive into `docker load` and compare every remote ID."""

    if not _private_output(receipt_path):
        raise TransferError(
            "transfer receipt must be stored under the umbrella .artifacts directory"
        )
    lock = _load_lock(lock_path.resolve())
    facts = _remote_facts(ssh)
    images = [*lock["images"], *lock.get("externalImages", {}).values()]
    references = [row["image"] if "image" in row else row["localImage"] for row in images]
    required = int(_locked_size_bytes(lock) * 1.10) + runtime_reserve_bytes
    if facts["rootFreeBytes"] < required:
        raise TransferError(
            f"remote root has {facts['rootFreeBytes'] / GIB:.2f} GiB free; measured lock requires at least {required / GIB:.2f} GiB"
        )
    host_key_value = next(
        (
            ssh[index + 1].split("=", 1)[1]
            for index, value in enumerate(ssh[:-1])
            if value == "-o" and ssh[index + 1].startswith("HostKeyAlias=")
        ),
        "unknown",
    )
    receipt: dict[str, Any] = {
        "schemaVersion": "cyrene-teams-cloud-image-transfer-v1",
        "taskId": lock["taskId"],
        "status": "PREFLIGHT_READY",
        "target": {"hostKeyAlias": host_key_value, "architecture": facts["architecture"]},
        "hostFactsBefore": facts,
        "lockedImageCount": len(images),
        "estimatedLayerBytes": _locked_size_bytes(lock),
        "runtimeReserveBytes": runtime_reserve_bytes,
        "images": [
            {"image": ref, "imageId": row["imageId"]} for ref, row in zip(references, images)
        ],
        "recordedAt": datetime.now(UTC).isoformat(),
    }
    if not execute:
        return receipt

    docker_save = subprocess.Popen(
        ["docker", "save", *references], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    ssh_process = subprocess.Popen(
        [*ssh, "sudo -n docker load"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if docker_save.stdout is None or ssh_process.stdin is None:
        docker_save.kill()
        ssh_process.kill()
        raise TransferError("stream pipe could not be created")
    try:
        while True:
            chunk = docker_save.stdout.read(1024 * 1024)
            if not chunk:
                break
            ssh_process.stdin.write(chunk)
        ssh_process.stdin.close()
        save_code = docker_save.wait()
        ssh_code = ssh_process.wait()
    except (BrokenPipeError, OSError) as error:
        docker_save.kill()
        ssh_process.kill()
        docker_save.wait()
        ssh_process.wait()
        raise TransferError("image stream failed; no Compose service was started") from error
    if save_code != 0 or ssh_code != 0:
        raise TransferError(
            "image stream or remote Docker load failed; no Compose service was started"
        )

    for row, reference in zip(images, references):
        observed = _remote(
            ssh, ["sudo", "-n", "docker", "image", "inspect", "--format", "{{.Id}}", reference]
        ).strip()
        if observed != row["imageId"]:
            raise TransferError(f"remote image ID read-back failed for {reference}")
    after = _remote_facts(ssh)
    if after["rootFreeBytes"] < runtime_reserve_bytes:
        raise TransferError(
            "images loaded, but root filesystem runtime reserve is too low; Compose must remain stopped"
        )
    receipt["status"] = "LOADED_AND_READ_BACK"
    receipt["hostFactsAfter"] = after
    receipt["dockerSaveExitCode"] = save_code
    receipt["dockerLoadExitCode"] = ssh_code
    return receipt


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    """Atomically store content-free image transfer evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    """Read-only preflight or explicitly authorized streaming image import."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--ssh-user", default="DH")
    parser.add_argument("--host-key-alias", required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--runtime-reserve-gib", type=float, default=2.0)
    parser.add_argument(
        "--execute", action="store_true", help="stream locked image bytes into the exact SSH target"
    )
    arguments = parser.parse_args()
    try:
        ssh = _ssh_base(
            arguments.ssh_target,
            arguments.ssh_user,
            arguments.host_key_alias,
            arguments.identity_file.resolve(),
        )
        receipt = transfer(
            arguments.lock.resolve(),
            arguments.receipt.resolve(),
            ssh,
            execute=arguments.execute,
            runtime_reserve_bytes=int(arguments.runtime_reserve_gib * GIB),
        )
        _write_receipt(arguments.receipt.resolve(), receipt)
        print(
            json.dumps(
                {
                    "status": receipt["status"],
                    "imageCount": receipt["lockedImageCount"],
                    "target": receipt["target"],
                },
                sort_keys=True,
            )
        )
    except (OSError, TransferError, BuildError, subprocess.SubprocessError) as error:
        print(f"ARM64 image transfer blocked: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
