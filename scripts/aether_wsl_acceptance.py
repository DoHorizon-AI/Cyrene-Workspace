"""
┌──────────────────────────────────────────────────────────────────────┐
│  📄 aether_wsl_acceptance.py                                         │
│  Module: scripts.aether_wsl_acceptance                               │
│  Role: Prepare and record isolated WSL acceptance resources.          │
│                                                                      │
│  模块职责：准备隔离的 WSL 验收资源并记录脱敏验收证据                         │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "tooling/aether-wsl-acceptance/compose.yaml"
STATE_DIR = Path("/home/baijin/.local/state/cyrene-aether-wsl-acceptance")
PRIVATE_STATE_PARENT = Path("/home/baijin/.local/state")
PROJECT_NAME = "cyrene-aether-wsl-acceptance"
TASK_LABEL = "io.cyrene.task"
TASK_LABEL_VALUE = "aether-wsl-acceptance-20261031"
LOCAL_OWNER_ACTOR_ID = "0722b287-fb57-500d-9a17-7c48f2c524ef"
LEGACY_LOCAL_OWNER_ACTOR_ID = "aether-local-acceptance"
TOOLCHAIN_DIR = STATE_DIR / "toolchain"
EVIDENCE_DIR = STATE_DIR / "evidence"
DATABASE_ENV = STATE_DIR / "postgres.env"
RESERVED_PORTS = (18680, 18681, 18682, 18684, 18685)
TASK_SUBNET = ipaddress.ip_network("10.247.253.0/24")
NODE_VERSION = "24.13.0"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
NODE_SHA256 = "e798599612f4bb71333a3397ab0d095fd62214e115aea45aa858a145fc72d67e"
PNPM_VERSION = "11.7.0"
PNPM_INTEGRITY = "sha512-GcyFLBIMcSV2DyRD7mvgyltA+fUFmN4aCaHxd1A+AQ5Xwjx3ZG4B52HeWb+HT7IqM5jDOrlpH8E+uUa28PTWIA=="
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_FILE_BYTES = 50 * 1024 * 1024
MAX_PRIVATE_ENV_BYTES = 64 * 1024
SAFE_EVIDENCE_SUFFIXES = frozenset(
    {
        ".csv",
        ".html",
        ".json",
        ".jsonl",
        ".log",
        ".md",
        ".png",
        ".jpg",
        ".jpeg",
        ".txt",
        ".webp",
        ".zip",
    }
)
MODEL_ARTIFACTS = (
    {
        "name": "Qwen2.5 0.5B Instruct Q4_K_M",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "revision": "9217f5db79a29953eb74d5343926648285ec7e67",
        "relative_path": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "destination": "qwen/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "size": 491400032,
        "sha256": "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db",
        "license": "Apache-2.0",
    },
    {
        "name": "multilingual-e5-small quantized ONNX",
        "model_id": "intfloat/multilingual-e5-small",
        "revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        "relative_path": "onnx/model_qint8_avx512_vnni.onnx",
        "destination": "e5/model_qint8_avx512_vnni.onnx",
        "size": 118346824,
        "sha256": "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88",
        "license": "MIT",
    },
    {
        "name": "multilingual-e5-small tokenizer",
        "model_id": "intfloat/multilingual-e5-small",
        "revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        "relative_path": "onnx/tokenizer.json",
        "destination": "e5/tokenizer.json",
        "size": 17082730,
        "sha256": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
        "license": "MIT",
    },
)
SPEECH_ARTIFACTS = (
    {
        "name": "SenseVoice sherpa-onnx int8 model",
        "model_id": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "revision": "2365baeacb507f821a0c8120fcee3d484dba7a07",
        "relative_path": "model.int8.onnx",
        "destination": "speech/model.int8.onnx",
        "size": 239233841,
        "sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
        "license": "FunASR Model Open Source License Agreement v1.1; retain model attribution/name",
    },
    {
        "name": "SenseVoice tokenizer",
        "model_id": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "revision": "2365baeacb507f821a0c8120fcee3d484dba7a07",
        "relative_path": "tokens.txt",
        "destination": "speech/tokens.txt",
        "size": 315894,
        "sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        "git_blob_sha1": "2cfc92fc2ff26aaa690b7c01fd96b41109413881",
        "license": "FunASR Model Open Source License Agreement v1.1; retain model attribution/name",
    },
    {
        "name": "official sherpa-onnx Chinese test audio",
        "model_id": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "revision": "2365baeacb507f821a0c8120fcee3d484dba7a07",
        "relative_path": "test_wavs/zh.wav",
        "destination": "speech/zh.wav",
        "size": 178988,
        "sha256": "b77f1794fe374a0ba1ee1dc458bfaf9349496cbbfc32780c50ba3c5a7ad8e373",
        "git_blob_sha1": "24a2bfc5c039f9d97db2cf1f2461699b582e0a90",
        "license": "Official sherpa-onnx model repository test fixture; source revision pinned",
    },
)
SPEECH_WHEELS = (
    {
        "name": "sherpa-onnx",
        "version": "1.13.8",
        "filename": "sherpa_onnx-1.13.8-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        "url": "https://files.pythonhosted.org/packages/13/78/2f712b7408ddbb64614c20388912414705b728516224e737b63f2adec94a/sherpa_onnx-1.13.8-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        "size": 4400575,
        "sha256": "6949773017647febc0c3696dffb2c67dd3febd4737b87e3dd135a42773704a06",
    },
    {
        "name": "sherpa-onnx-core",
        "version": "1.13.8",
        "filename": "sherpa_onnx_core-1.13.8-py3-none-manylinux2014_x86_64.whl",
        "url": "https://files.pythonhosted.org/packages/b6/ca/e27c1fb5c54b181d4a5450f3d5c789d9da02eb42dc9b20cfdab5dda4c864/sherpa_onnx_core-1.13.8-py3-none-manylinux2014_x86_64.whl",
        "size": 10642497,
        "sha256": "4da90acf435373d7b2ba9cc0be806e7e274d28f63f5780880b5dea6721626e36",
    },
    {
        "name": "numpy",
        "version": "2.5.4",
        "filename": "numpy-2.5.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
        "url": "https://files.pythonhosted.org/packages/43/a3/c799c62e19c337e6d3770b08e475887fb30ce8477d3c09efca6b2f0228a6/numpy-2.5.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
        "size": 16727283,
        "sha256": "fbde6962867ee75b48b0ee29b2b9372ec5d617799dbaf38e82dc0596f2f7738a",
    },
    {
        "name": "grpcio",
        "version": "1.62.3",
        "filename": "grpcio-1.62.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "url": "https://files.pythonhosted.org/packages/82/2e/43218874d1852af1ea9801a2be62cc596ddd45984e7adba0fb9f66393c81/grpcio-1.62.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "size": 5492246,
        "sha256": "ac9783d5679c8da612465168c820fd0b916e70ec5496c840bddba0be7f2d124c",
    },
    {
        "name": "protobuf",
        "version": "4.25.9",
        "filename": "protobuf-4.25.9-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/16/28/d5065b212685875d3924bcdb3201cbf467cb4d58a18aa19a8dfd99ea80a9/protobuf-4.25.9-py3-none-any.whl",
        "size": 156822,
        "sha256": "d49b615e7c935194ac161f0965699ac84df6112c378e05ec53da65d2e4cbb6d4",
    },
    {
        "name": "imageio-ffmpeg",
        "version": "0.7.0",
        "filename": "imageio_ffmpeg-0.7.0-py3-none-manylinux2014_x86_64.whl",
        "url": "https://files.pythonhosted.org/packages/04/96/15fe7ef562f7494c358cbd9b521d2da82d7a331b5513703756166478bd5f/imageio_ffmpeg-0.7.0-py3-none-manylinux2014_x86_64.whl",
        "size": 29498414,
        "sha256": "4e1f36bef7bedd4bc5dc25f60a8345d88f57133fa1e754f09b411ac61b854408",
    },
)
BROWSER_PACKAGES = (
    (
        "libnspr4",
        "2:4.35-1.1build1",
        "e579e72d091f6c7a13f5a756c31065b15aae5b81840d61b069355aa2283c07b4",
    ),
    (
        "libnss3",
        "2:3.98-1ubuntu0.3",
        "e11fdf8c09a068d0f0c3fd90ed971253dfed7227cc20d3d487fd615d4771f1bb",
    ),
    (
        "libasound2t64",
        "1.2.11-1ubuntu0.3",
        "5d17bfb5683eb99f1be951fe48e1be5bf3c52391913b6b74a8aecf9b4f5e779f",
    ),
    (
        "libasound2-data",
        "1.2.11-1ubuntu0.3",
        "0c26896193480d9dbb055e85e8ddfb9c9f34cf19afee340987cd6311e2886fc1",
    ),
)


class AcceptanceError(RuntimeError):
    """A safe, user-facing acceptance setup error without command output."""


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 30,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Runs a subprocess while withholding its output, which may contain secrets."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(f"{label} could not complete ({type(exc).__name__}).") from None
    if result.returncode != 0:
        raise AcceptanceError(f"{label} failed (exit {result.returncode}).")
    return result


def _compose_env() -> dict[str, str]:
    """Builds the environment needed by the task-scoped Compose project."""
    env = os.environ.copy()
    env["CYRENE_AETHER_ACCEPTANCE_STATE"] = str(STATE_DIR)
    env["CYRENE_AETHER_UID"] = str(os.getuid())
    env["CYRENE_AETHER_GID"] = str(os.getgid())
    env["BUILDX_BUILDER"] = "default"
    return env


def _compose(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Invokes only the fixed local acceptance Compose project."""
    if not shutil.which("docker"):
        raise AcceptanceError("docker is not installed or is absent from PATH.")
    command = [
        "docker",
        "compose",
        "--project-name",
        PROJECT_NAME,
        "--file",
        str(COMPOSE_FILE),
        *args,
    ]
    return _run(command, env=_compose_env(), timeout=timeout, label="Local acceptance Compose")


def _validate_private_state_parent() -> None:
    """Rejects links or unexpected owners in the fixed private-state parent chain."""
    components = (
        Path("/"),
        Path("/home"),
        Path("/home/baijin"),
        Path("/home/baijin/.local"),
        PRIVATE_STATE_PARENT,
    )
    for directory in components:
        try:
            info = directory.lstat()
        except OSError:
            raise AcceptanceError(
                "A fixed parent of the private task state is unavailable."
            ) from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AcceptanceError(
                "A fixed parent of the private task state is not a real directory."
            )
        if directory not in {Path("/"), Path("/home")} and info.st_uid != os.getuid():
            raise AcceptanceError(
                "A fixed parent of the private task state has an unexpected owner."
            )


def _secure_directory(path: Path) -> None:
    """Creates a private task directory without following a symlink component."""
    _validate_private_state_parent()
    normalized = Path(os.path.abspath(path))
    try:
        relative = normalized.relative_to(STATE_DIR)
    except ValueError:
        raise AcceptanceError(
            "Private task directories must remain under the fixed state root."
        ) from None
    directories = [STATE_DIR]
    current = STATE_DIR
    for component in relative.parts:
        if component == ".":
            continue
        current = current / component
        directories.append(current)
    for directory in directories:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise AcceptanceError("A private task directory could not be created safely.") from None
        try:
            info = directory.lstat()
        except OSError:
            raise AcceptanceError("A private task directory is unavailable.") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AcceptanceError("A private task directory cannot be a symlink or special file.")
        if info.st_uid != os.getuid():
            raise AcceptanceError("A private task directory has an unexpected owner.")
        try:
            directory_fd = os.open(
                directory,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError:
            raise AcceptanceError("A private task directory changed during validation.") from None
        try:
            verified = os.fstat(directory_fd)
            if not stat.S_ISDIR(verified.st_mode) or verified.st_uid != os.getuid():
                raise AcceptanceError(
                    "A private task directory changed ownership during validation."
                )
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)


def _open_private_regular_file(path: Path, flags: int, mode: int, label: str) -> int:
    """Opens an owned regular file without following its final path component."""
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    except OSError:
        raise AcceptanceError(f"The private {label} file could not be opened safely.") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise AcceptanceError(f"The private {label} path is not an owned regular file.")
        os.fchmod(fd, mode)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_private_json(path: Path, label: str) -> dict[str, Any]:
    """Reads one task-owned JSON receipt through a no-follow regular-file descriptor."""
    fd = _open_private_regular_file(path, os.O_RDONLY, 0o600, label)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as receipt_file:
            value = json.load(receipt_file)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AcceptanceError(f"The private {label} JSON is malformed or unreadable.") from None
    if not isinstance(value, dict):
        raise AcceptanceError(f"The private {label} JSON must be an object.")
    return value


def _read_private_env(path: Path, label: str) -> dict[str, str]:
    """Read one small task-owned env file without displaying its values."""
    fd = _open_private_regular_file(path, os.O_RDONLY, 0o600, label)
    try:
        with os.fdopen(fd, "rb") as environment_file:
            if os.fstat(environment_file.fileno()).st_size > MAX_PRIVATE_ENV_BYTES:
                raise AcceptanceError(f"The private {label} file exceeds its size bound.")
            raw = environment_file.read(MAX_PRIVATE_ENV_BYTES + 1)
        if len(raw) > MAX_PRIVATE_ENV_BYTES:
            raise AcceptanceError(f"The private {label} file exceeds its size bound.")
        lines = raw.decode("utf-8").splitlines()
    except AcceptanceError:
        raise
    except (OSError, UnicodeError):
        raise AcceptanceError(f"The private {label} file is malformed or unreadable.") from None
    if len(lines) > 1024:
        raise AcceptanceError(f"The private {label} file exceeds its size bound.")
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
            raise AcceptanceError(f"The private {label} file contains an invalid entry.")
        if name in values or "\n" in value or "\r" in value:
            raise AcceptanceError(f"The private {label} file contains duplicate or invalid values.")
        values[name] = value
    return values


def _ensure_state() -> None:
    """Creates private state locations without touching repository artifacts."""
    for path in (STATE_DIR, TOOLCHAIN_DIR, EVIDENCE_DIR):
        _secure_directory(path)


def _ensure_postgres_env() -> None:
    """Creates or validates the private local PostgreSQL credentials file."""
    _ensure_state()
    lock_path = STATE_DIR / ".bootstrap.lock"
    fd = _open_private_regular_file(
        lock_path, os.O_CREAT | os.O_RDWR, 0o600, "PostgreSQL bootstrap lock"
    )
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            DATABASE_ENV.lstat()
            env_missing = False
        except FileNotFoundError:
            env_missing = True
        except OSError:
            raise AcceptanceError(
                "The private PostgreSQL environment path is unavailable."
            ) from None
        if env_missing:
            password = secrets.token_urlsafe(36)
            content = (
                "POSTGRES_USER=cyrene_aether\n"
                f"POSTGRES_PASSWORD={password}\n"
                "POSTGRES_DB=cyrene_aether\n"
            ).encode()
            env_fd = _open_private_regular_file(
                DATABASE_ENV,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
                "PostgreSQL environment",
            )
            try:
                os.write(env_fd, content)
                os.fsync(env_fd)
            finally:
                os.close(env_fd)
        env_fd = _open_private_regular_file(
            DATABASE_ENV, os.O_RDONLY, 0o600, "PostgreSQL environment"
        )
        values: dict[str, str] = {}
        try:
            with os.fdopen(env_fd, "r", encoding="utf-8") as environment_file:
                for line in environment_file.read().splitlines():
                    key, separator, value = line.partition("=")
                    if not separator or key in values:
                        raise AcceptanceError(
                            "The private PostgreSQL environment file is malformed."
                        )
                    values[key] = value
        except UnicodeError:
            raise AcceptanceError("The private PostgreSQL environment file is malformed.") from None
        password = values.get("POSTGRES_PASSWORD", "")
        if (
            set(values) != {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
            or values.get("POSTGRES_USER") != "cyrene_aether"
            or values.get("POSTGRES_DB") != "cyrene_aether"
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,}", password)
        ):
            raise AcceptanceError("The private PostgreSQL environment file failed validation.")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ensure_postgres_data_dir() -> None:
    """Checks the private bind mount after PostgreSQL may have taken ownership as uid 999."""
    path = STATE_DIR / "postgres-data"
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            os.mkdir(path, 0o700)
            info = path.lstat()
        except OSError:
            raise AcceptanceError("The PostgreSQL data path could not be created safely.") from None
    except OSError:
        raise AcceptanceError("The PostgreSQL data path is unavailable.") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AcceptanceError("The PostgreSQL data path is not a regular task directory.")
    if info.st_uid not in {os.getuid(), 999}:
        raise AcceptanceError("The PostgreSQL data directory has an unexpected owner.")
    if info.st_uid == os.getuid():
        try:
            directory_fd = os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError:
            raise AcceptanceError(
                "The PostgreSQL data directory changed during validation."
            ) from None
        try:
            verified = os.fstat(directory_fd)
            if not stat.S_ISDIR(verified.st_mode) or verified.st_uid != os.getuid():
                raise AcceptanceError("The PostgreSQL data directory changed ownership.")
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)
    elif info.st_mode & 0o077:
        raise AcceptanceError(
            "The PostgreSQL-owned data directory is accessible to group or other users."
        )


def _wsl_facts() -> dict[str, str | bool]:
    """Reads non-sensitive host facts used to constrain acceptance to WSL2 x64."""
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    version_match = re.search(r'^VERSION_ID="?([^"\n]+)', os_release, re.MULTILINE)
    kernel = platform.release()
    return {
        "linux": sys.platform.startswith("linux"),
        "ubuntu_version": version_match.group(1) if version_match else "unknown",
        "wsl2": "microsoft-standard-WSL2" in kernel,
        "architecture": platform.machine(),
    }


def _require_wsl2_x64() -> None:
    """Rejects non-target hosts before creating local Docker resources."""
    facts = _wsl_facts()
    if not (
        facts["linux"] is True
        and facts["ubuntu_version"] == "24.04"
        and facts["wsl2"] is True
        and facts["architecture"] == "x86_64"
    ):
        raise AcceptanceError("This acceptance project is restricted to Ubuntu 24.04 WSL2 x86_64.")


def _docker_context_host() -> str:
    """Returns the active Docker endpoint without exposing daemon configuration."""
    result = _run(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        timeout=10,
        label="Docker context inspection",
    )
    return result.stdout.strip()


def _require_local_docker() -> None:
    """Ensures Docker Compose is connected to a local Unix socket."""
    endpoint = _docker_context_host()
    if not endpoint.startswith("unix://"):
        raise AcceptanceError(
            "The active Docker context is not a local Unix socket; refusing to start services."
        )
    _run(
        ["docker", "compose", "version", "--short"],
        timeout=10,
        label="Docker Compose version check",
    )


def _port_available(port: int) -> bool:
    """Checks whether a loopback TCP port currently accepts connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.15)
        return client.connect_ex(("127.0.0.1", port)) != 0


def _task_containers() -> list[dict[str, Any]]:
    """Returns metadata for containers in this fixed project without emitting environment values."""
    result = _run(
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={PROJECT_NAME}",
            "--format",
            "{{json .}}",
        ],
        timeout=15,
        label="Acceptance container inventory",
    )
    resources = []
    for line in result.stdout.splitlines():
        if line.strip():
            resources.append(json.loads(line))
    return resources


def _verify_project_ownership() -> list[dict[str, Any]]:
    """Checks every existing project container carries this task's unique label."""
    resources = _task_containers()
    for resource in resources:
        inspected = _run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", resource["ID"]],
            timeout=10,
            label="Acceptance container ownership check",
        )
        labels = json.loads(inspected.stdout)
        if labels.get(TASK_LABEL) != TASK_LABEL_VALUE:
            raise AcceptanceError(
                "The fixed Compose project name is already used by an unowned container; refusing to change it."
            )
    return resources


def _verify_task_subnet_available() -> None:
    """Rejects an explicit task subnet if a live route or Docker network overlaps it."""
    network_ids = _run(
        ["docker", "network", "ls", "-q"], timeout=15, label="Docker network inventory"
    ).stdout.split()
    task_network_found = False
    if network_ids:
        networks = json.loads(
            _run(
                ["docker", "network", "inspect", *network_ids],
                timeout=20,
                label="Docker subnet inventory",
            ).stdout
        )
        for network in networks:
            for item in network.get("IPAM", {}).get("Config") or []:
                subnet_value = item.get("Subnet")
                if not subnet_value or not ipaddress.ip_network(
                    subnet_value, strict=False
                ).overlaps(TASK_SUBNET):
                    continue
                labels = network.get("Labels") or {}
                is_task_network = (
                    network.get("Name", "").startswith(f"{PROJECT_NAME}_")
                    and labels.get(TASK_LABEL) == TASK_LABEL_VALUE
                    and ipaddress.ip_network(subnet_value, strict=False) == TASK_SUBNET
                )
                if is_task_network:
                    task_network_found = True
                    continue
                raise AcceptanceError(
                    f"Task subnet {TASK_SUBNET} overlaps existing Docker network {network.get('Name')}."
                )
    routes = _run(
        ["ip", "-o", "route", "show"], timeout=10, label="WSL route inventory"
    ).stdout.splitlines()
    for route in routes:
        destination = route.split(maxsplit=1)[0]
        if destination == "default" or ":" in destination:
            continue
        try:
            route_network = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            continue
        if (
            route_network.prefixlen
            and route_network.overlaps(TASK_SUBNET)
            and not (task_network_found and route_network == TASK_SUBNET)
        ):
            raise AcceptanceError(
                f"Task subnet {TASK_SUBNET} overlaps an existing WSL route {route_network}."
            )


def _download(url: str, destination: Path, *, maximum: int) -> None:
    """Downloads a bounded HTTPS artifact and rejects unexpected redirect hosts."""
    parsed = urllib.parse.urlparse(url)
    allowed_hosts = {
        parsed.hostname,
        "nodejs.org",
        "registry.npmjs.org",
        "huggingface.co",
        "cas-bridge.xethub.hf.co",
        "us.aws.cdn.hf.co",
        "files.pythonhosted.org",
    }
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise AcceptanceError("Refusing a non-HTTPS or unapproved artifact URL.")
    request = urllib.request.Request(
        url, headers={"User-Agent": "Cyrene-Aether-WSL-Acceptance/1.0"}
    )
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            destination.open("wb") as output,
        ):
            final_host = urllib.parse.urlparse(response.geturl()).hostname
            if final_host not in allowed_hosts:
                raise AcceptanceError("Artifact download redirected to an unapproved host.")
            received = 0
            while block := response.read(1024 * 1024):
                received += len(block)
                if received > maximum:
                    raise AcceptanceError("Artifact exceeded the configured download size bound.")
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    except AcceptanceError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        destination.unlink(missing_ok=True)
        raise AcceptanceError(f"Artifact download failed ({type(exc).__name__}).") from None


def _sha256(path: Path) -> str:
    """Calculates a streaming SHA-256 digest for an artifact file."""
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_blob_sha1(path: Path) -> str:
    """Calculates Git's object digest for a small pinned, non-LFS source file."""
    digest = hashlib.sha1()
    size = path.stat().st_size
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _install_node(temp_root: Path) -> tuple[Path, str]:
    """Downloads, verifies, and installs the pinned Node distribution privately."""
    node_root = TOOLCHAIN_DIR / f"node-v{NODE_VERSION}-linux-x64"
    node_binary = node_root / "bin/node"
    if node_binary.exists():
        result = _run([str(node_binary), "--version"], timeout=10, label="Task Node version check")
        if result.stdout.strip() != f"v{NODE_VERSION}":
            raise AcceptanceError("The task-local Node installation has an unexpected version.")
        return node_root, _sha256(node_binary)

    base_url = f"https://nodejs.org/dist/v{NODE_VERSION}"
    checksum_file = temp_root / "SHASUMS256.txt"
    _download(f"{base_url}/SHASUMS256.txt", checksum_file, maximum=1024 * 1024)
    lines = checksum_file.read_text(encoding="ascii").splitlines()
    expected = next((line.split()[0] for line in lines if line.endswith(NODE_ARCHIVE)), None)
    if expected != NODE_SHA256:
        raise AcceptanceError(
            "Node's official checksum differs from the pinned digest; stop for review."
        )

    archive = temp_root / NODE_ARCHIVE
    _download(f"{base_url}/{NODE_ARCHIVE}", archive, maximum=MAX_DOWNLOAD_BYTES)
    if _sha256(archive) != NODE_SHA256:
        raise AcceptanceError("The downloaded Node archive failed its official SHA-256 check.")

    extraction_root = temp_root / "extract"
    extraction_root.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, "r:xz") as bundle:
            prefix = f"node-v{NODE_VERSION}-linux-x64/"
            for member in bundle.getmembers():
                member_path = PurePosixPath(member.name)
                expected_root = prefix.rstrip("/")
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or (member.name != expected_root and not member.name.startswith(prefix))
                ):
                    raise AcceptanceError(
                        "Node archive contains a path outside its expected release directory."
                    )
            bundle.extractall(extraction_root, filter="data")
    except AcceptanceError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise AcceptanceError(f"Node archive extraction failed ({type(exc).__name__}).") from None
    extracted_root = extraction_root / f"node-v{NODE_VERSION}-linux-x64"
    if not (extracted_root / "bin/node").is_file():
        raise AcceptanceError("Node archive did not contain the expected executable.")
    os.replace(extracted_root, node_root)
    os.chmod(node_root / "bin/node", 0o755)
    return node_root, _sha256(node_root / "bin/node")


def _install_pnpm(node_root: Path, temp_root: Path) -> tuple[Path, str]:
    """Verifies the pnpm tarball before extracting it to a task-local prefix."""
    pnpm_root = TOOLCHAIN_DIR / f"pnpm-{PNPM_VERSION}"
    pnpm_binary = pnpm_root / "bin/pnpm"
    if pnpm_binary.exists():
        result = _run(
            [str(node_root / "bin/node"), str(pnpm_binary), "--version"],
            timeout=15,
            label="Task pnpm version check",
        )
        if result.stdout.strip() != PNPM_VERSION:
            raise AcceptanceError("The task-local pnpm installation has an unexpected version.")
        return pnpm_root, _sha256(pnpm_root / "lib/node_modules/pnpm/bin/pnpm.mjs")

    metadata_path = temp_root / "pnpm-metadata.json"
    _download(
        f"https://registry.npmjs.org/pnpm/{PNPM_VERSION}",
        metadata_path,
        maximum=2 * 1024 * 1024,
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    dist = metadata.get("dist", {})
    tarball_url = dist.get("tarball", "")
    integrity = dist.get("integrity", "")
    if (
        integrity != PNPM_INTEGRITY
        or tarball_url != f"https://registry.npmjs.org/pnpm/-/pnpm-{PNPM_VERSION}.tgz"
    ):
        raise AcceptanceError(
            "pnpm registry integrity differs from the pinned official package metadata."
        )
    tarball = temp_root / f"pnpm-{PNPM_VERSION}.tgz"
    _download(tarball_url, tarball, maximum=MAX_DOWNLOAD_BYTES)
    digest = base64.b64encode(hashlib.sha512(tarball.read_bytes()).digest()).decode("ascii")
    if f"sha512-{digest}" != PNPM_INTEGRITY:
        raise AcceptanceError("The downloaded pnpm tarball failed its npm integrity check.")

    staging_prefix = temp_root / "pnpm-prefix"
    staging_prefix.mkdir(mode=0o700)
    extraction_root = temp_root / "pnpm-extract"
    extraction_root.mkdir(mode=0o700)
    try:
        with tarfile.open(tarball, "r:gz") as bundle:
            for member in bundle.getmembers():
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or (member.name != "package" and not member.name.startswith("package/"))
                ):
                    raise AcceptanceError(
                        "pnpm tarball contains a path outside its expected package directory."
                    )
            bundle.extractall(extraction_root, filter="data")
    except AcceptanceError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise AcceptanceError(f"pnpm archive extraction failed ({type(exc).__name__}).") from None
    package_root = extraction_root / "package"
    package_metadata = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    if package_metadata.get("version") != PNPM_VERSION:
        raise AcceptanceError("pnpm package metadata does not match the pinned version.")
    package_bin = package_metadata.get("bin", {}).get("pnpm")
    pnpx_bin = package_metadata.get("bin", {}).get("pnpx")
    if package_bin != "bin/pnpm.mjs" or pnpx_bin != "bin/pnpx.mjs":
        raise AcceptanceError("pnpm package entrypoints differ from the expected distribution.")
    package_destination = staging_prefix / "lib/node_modules/pnpm"
    package_destination.parent.mkdir(mode=0o700, parents=True)
    os.replace(package_root, package_destination)
    (staging_prefix / "bin").mkdir(mode=0o700)
    os.symlink("../lib/node_modules/pnpm/bin/pnpm.mjs", staging_prefix / "bin/pnpm")
    os.symlink("../lib/node_modules/pnpm/bin/pnpx.mjs", staging_prefix / "bin/pnpx")
    staging_binary = staging_prefix / "bin/pnpm"
    result = _run(
        [str(node_root / "bin/node"), str(staging_binary), "--version"],
        timeout=30,
        label="Task pnpm version check",
    )
    if result.stdout.strip() != PNPM_VERSION:
        raise AcceptanceError(
            "The installed task-local pnpm version does not match the requested version."
        )
    if pnpm_root.exists():
        raise AcceptanceError(
            "A partial task-local pnpm directory exists; preserve it for inspection and review."
        )
    os.replace(staging_prefix, pnpm_root)
    return pnpm_root, _sha256(pnpm_root / "lib/node_modules/pnpm/bin/pnpm.mjs")


def _verify_postgres_isolation(container_id: str) -> None:
    """Verifies the running database uses only task-owned, private local resources."""
    details = json.loads(
        _run(
            ["docker", "inspect", "--format", "{{json .}}", container_id],
            timeout=10,
            label="PostgreSQL isolation inspection",
        ).stdout
    )
    labels = details.get("Config", {}).get("Labels") or {}
    if labels.get(TASK_LABEL) != TASK_LABEL_VALUE:
        raise AcceptanceError("The PostgreSQL container is missing its task ownership label.")
    expected_image = (
        "pgvector/pgvector:pg17@sha256:"
        "7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8"
    )
    if details.get("Config", {}).get("Image") != expected_image:
        raise AcceptanceError("The PostgreSQL container image does not match the pinned digest.")
    bindings = details.get("HostConfig", {}).get("PortBindings")
    if bindings and any(binding for binding in bindings.values()):
        raise AcceptanceError("The PostgreSQL container unexpectedly publishes a host port.")
    expected_data = str((STATE_DIR / "postgres-data").resolve())
    mounts = details.get("Mounts", [])
    if not any(
        mount.get("Type") == "bind"
        and str(Path(mount.get("Source", "")).resolve()) == expected_data
        and mount.get("Destination") == "/var/lib/postgresql/data"
        for mount in mounts
    ):
        raise AcceptanceError(
            "The PostgreSQL data mount is not the task's dedicated state directory."
        )
    networks = details.get("NetworkSettings", {}).get("Networks", {})
    if len(networks) != 1:
        raise AcceptanceError(
            "The PostgreSQL container is attached to an unexpected number of networks."
        )
    network_name = next(iter(networks))
    prefix = f"{PROJECT_NAME}_"
    if not network_name.startswith(prefix):
        raise AcceptanceError(
            "The PostgreSQL container is attached to a network outside this Compose project."
        )
    network = json.loads(
        _run(
            ["docker", "network", "inspect", "--format", "{{json .}}", network_name],
            timeout=10,
            label="PostgreSQL network inspection",
        ).stdout
    )
    if (
        not network.get("Internal")
        or (network.get("Labels") or {}).get(TASK_LABEL) != TASK_LABEL_VALUE
    ):
        raise AcceptanceError("The PostgreSQL network is not an internal, task-labelled network.")


def _postgres_connection() -> tuple[str, str]:
    """Builds a private test DSN from the task container without exposing credentials."""
    matches = _run(
        [
            "docker",
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={PROJECT_NAME}",
            "--filter",
            "label=com.docker.compose.service=postgres",
        ],
        timeout=15,
        label="Acceptance PostgreSQL lookup",
    ).stdout.split()
    if len(matches) != 1:
        raise AcceptanceError("The isolated PostgreSQL service is not running exactly once.")
    _verify_postgres_isolation(matches[0])
    network_details = json.loads(
        _run(
            ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", matches[0]],
            timeout=10,
            label="Acceptance PostgreSQL network lookup",
        ).stdout
    )
    network = next(iter(network_details.values()))
    address = network.get("IPAddress", "")
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise AcceptanceError(
            "The isolated PostgreSQL service has no task-network address."
        ) from None

    values = {}
    for line in DATABASE_ENV.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    password = values.get("POSTGRES_PASSWORD", "")
    if (
        not password
        or values.get("POSTGRES_USER") != "cyrene_aether"
        or values.get("POSTGRES_DB") != "cyrene_aether"
    ):
        raise AcceptanceError("The private PostgreSQL environment file failed validation.")
    dsn = (
        "postgresql://"
        + urllib.parse.quote(values["POSTGRES_USER"], safe="")
        + ":"
        + urllib.parse.quote(password, safe="")
        + "@"
        + address
        + ":5432/"
        + urllib.parse.quote(values["POSTGRES_DB"], safe="")
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        if client.connect_ex((address, 5432)) != 0:
            raise AcceptanceError(
                "The host test process cannot reach PostgreSQL on its private bridge address."
            )
    return dsn, password


def _write_private_log(name: str, content: str) -> Path:
    """Writes sanitized command output under the task's private evidence directory."""
    _secure_directory(EVIDENCE_DIR)
    path = EVIDENCE_DIR / name
    fd = _open_private_regular_file(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, "evidence log"
    )
    try:
        os.write(fd, content.encode("utf-8", errors="replace"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def _write_private_file(path: Path, content: str) -> Path:
    """Atomically writes a replaceable private task-state file with mode 0600."""
    _secure_directory(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError:
        raise AcceptanceError("A private task file is unavailable.") from None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.getuid()
    ):
        raise AcceptanceError("A private task file must be an owned regular file.")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = _open_private_regular_file(
        temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, "temporary task"
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    return path


def cmd_doctor(_: argparse.Namespace) -> None:
    """Reports non-sensitive WSL, Docker, port, and local model-runtime readiness."""
    facts = _wsl_facts()
    for key, value in facts.items():
        print(f"{key}={value}")
    if shutil.which("docker"):
        print(f"docker_context_local_unix={_docker_context_host().startswith('unix://')}")
        compose_version = _run(
            ["docker", "compose", "version", "--short"],
            timeout=10,
            label="Docker Compose version check",
        ).stdout.strip()
        print(f"docker_compose_version={compose_version}")
        print(f"existing_task_project_containers={len(_verify_project_ownership())}")
    else:
        print("docker_installed=False")
    for port in RESERVED_PORTS:
        print(f"loopback_port_{port}_available={_port_available(port)}")
    hf_cache = Path.home() / ".cache/huggingface/hub"
    qwen_cache = hf_cache / "models--Qwen--Qwen2.5-0.5B-Instruct"
    print(f"cached_qwen25_05b_present={qwen_cache.exists()}")
    print(
        f"cached_qwen25_05b_weight_present={any(qwen_cache.glob('snapshots/*/model.safetensors'))}"
    )
    for package in ("torch", "transformers", "onnxruntime", "fastembed", "funasr", "sherpa_onnx"):
        spec = __import__("importlib.util", fromlist=["find_spec"]).find_spec(package)
        print(f"global_python_{package}_installed={spec is not None}")
    print("external_platform_acceptance=DEFERRED_BY_USER/NOT_RUN")


def cmd_toolchain(_: argparse.Namespace) -> None:
    """Installs the pinned Node and pnpm versions under the task state directory."""
    _require_wsl2_x64()
    _ensure_state()
    temp_root_path = TOOLCHAIN_DIR / ".install-tmp"
    temp_root_path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(temp_root_path, 0o700)
    with tempfile.TemporaryDirectory(prefix="aether-toolchain-", dir=temp_root_path) as temp_name:
        temp_root = Path(temp_name)
        node_root, node_binary_sha = _install_node(temp_root)
        pnpm_root, pnpm_binary_sha = _install_pnpm(node_root, temp_root)
    manifest = {
        "node": {
            "version": NODE_VERSION,
            "archive": NODE_ARCHIVE,
            "source": f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}",
            "official_sha256": NODE_SHA256,
            "binary_sha256": node_binary_sha,
            "path": str(node_root),
        },
        "pnpm": {
            "version": PNPM_VERSION,
            "source": f"https://registry.npmjs.org/pnpm/{PNPM_VERSION}",
            "official_integrity": PNPM_INTEGRITY,
            "entrypoint_sha256": pnpm_binary_sha,
            "path": str(pnpm_root),
        },
        "installed_at_utc": datetime.now(UTC).isoformat(),
        "global_toolchain_modified": False,
    }
    manifest_path = TOOLCHAIN_DIR / "manifest.json"
    manifest_tmp = TOOLCHAIN_DIR / ".manifest.json.tmp"
    manifest_tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.chmod(manifest_tmp, 0o600)
    os.replace(manifest_tmp, manifest_path)
    _write_private_file(EVIDENCE_DIR / "toolchain.json", json.dumps(manifest, indent=2) + "\n")
    print(f"node={NODE_VERSION} path={node_root / 'bin/node'} sha256={node_binary_sha}")
    print(f"pnpm={PNPM_VERSION} path={pnpm_root / 'bin/pnpm'} sha256={pnpm_binary_sha}")
    print(f"toolchain_manifest={manifest_path}")


def _model_artifact_path(item: dict[str, Any]) -> Path:
    """Returns an artifact path while ensuring its parent remains task-private."""
    path = STATE_DIR / "models" / item["destination"]
    _secure_directory(path.parent)
    return path


def _prepare_model_artifact(item: dict[str, Any]) -> dict[str, Any]:
    """Downloads one pinned public model file and verifies size and SHA-256."""
    path = _model_artifact_path(item)
    source = (
        f"https://huggingface.co/{item['model_id']}/resolve/"
        f"{item['revision']}/{item['relative_path']}"
    )
    if path.is_symlink():
        raise AcceptanceError(f"A task-local model artifact must not be a symlink: {item['name']}.")
    if path.exists():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_size != item["size"]
            or _sha256(path) != item["sha256"]
        ):
            raise AcceptanceError(
                f"A task-local model artifact failed its pinned hash: {item['name']}."
            )
    else:
        with tempfile.TemporaryDirectory(prefix="model-download-", dir=path.parent) as temp_name:
            temporary = Path(temp_name) / "artifact.download"
            _download(source, temporary, maximum=item["size"] + 1024 * 1024)
            if temporary.stat().st_size != item["size"] or _sha256(temporary) != item["sha256"]:
                raise AcceptanceError(
                    f"A model download failed its pinned size/hash check: {item['name']}."
                )
            expected_blob_sha1 = item.get("git_blob_sha1")
            if expected_blob_sha1 and _git_blob_sha1(temporary) != expected_blob_sha1:
                raise AcceptanceError(
                    f"A model download failed its pinned source-object check: {item['name']}."
                )
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
    os.chmod(path, 0o600)
    expected_blob_sha1 = item.get("git_blob_sha1")
    actual_blob_sha1 = _git_blob_sha1(path) if expected_blob_sha1 else None
    if expected_blob_sha1 and actual_blob_sha1 != expected_blob_sha1:
        raise AcceptanceError(
            f"A task-local artifact failed its pinned source-object check: {item['name']}."
        )
    return {
        "name": item["name"],
        "model_id": item["model_id"],
        "revision": item["revision"],
        "source": source,
        "license": item["license"],
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        **({"git_blob_sha1": actual_blob_sha1} if actual_blob_sha1 else {}),
    }


def _has_avx512_vnni() -> bool:
    """Checks whether WSL exposes the instruction set required by the pinned E5 ONNX graph."""
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("flags"):
            return "avx512_vnni" in line.split(":", 1)[1].split()
    return False


def cmd_models_prepare(_: argparse.Namespace) -> None:
    """Fetches pinned, openly licensed CPU model artifacts into private task state."""
    _require_wsl2_x64()
    _ensure_state()
    if not _has_avx512_vnni():
        raise AcceptanceError(
            "The selected quantized E5 ONNX artifact requires AVX512 VNNI, which WSL does not expose."
        )
    artifacts = [_prepare_model_artifact(item) for item in MODEL_ARTIFACTS]
    manifest = {
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "source_policy": "official Hugging Face model repositories at immutable revisions",
        "runtime": {
            "llama_cpp_image": "ghcr.io/ggml-org/llama.cpp:server@sha256:d9e9e74ba3f7ef119b1939162b1631f99b75ec703c310d140cfa2fdac8907389",
            "embedding_image": "cyrene-aether/embedding-cpu:20261031",
            "embedding_python_base": "python:3.12-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487",
        },
        "artifacts": artifacts,
    }
    manifest_path = STATE_DIR / "models" / "manifest.json"
    temporary_path = manifest_path.with_name(".manifest.json.tmp")
    temporary_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, manifest_path)
    for item in artifacts:
        print(
            f"model_artifact={item['model_id']} revision={item['revision']} bytes={item['bytes']} sha256={item['sha256']}"
        )
    print(f"model_manifest={manifest_path}")


def _speech_site_packages() -> Path:
    """Returns the acceptance-only Python package extraction location."""
    path = TOOLCHAIN_DIR / "speech-site-packages"
    _secure_directory(path)
    return path


def _speech_wheel_path(item: dict[str, Any]) -> Path:
    """Returns a private wheelhouse path for one pinned speech runtime wheel."""
    directory = STATE_DIR / "toolchain" / "speech-wheelhouse"
    _secure_directory(directory)
    return directory / item["filename"]


def _prepare_speech_wheel(item: dict[str, Any]) -> dict[str, Any]:
    """Downloads one pinned PyPI wheel and checks its exact published digest."""
    path = _speech_wheel_path(item)
    if path.is_symlink():
        raise AcceptanceError(f"A task-local speech wheel must not be a symlink: {item['name']}.")
    if path.exists():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_size != item["size"]
            or _sha256(path) != item["sha256"]
        ):
            raise AcceptanceError(
                f"A task-local speech wheel failed its pinned hash: {item['name']}."
            )
    else:
        with tempfile.TemporaryDirectory(prefix="speech-wheel-", dir=path.parent) as temp_name:
            temporary = Path(temp_name) / item["filename"]
            _download(item["url"], temporary, maximum=item["size"] + 1024 * 1024)
            if temporary.stat().st_size != item["size"] or _sha256(temporary) != item["sha256"]:
                raise AcceptanceError(f"A speech wheel failed its pinned hash: {item['name']}.")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
    os.chmod(path, 0o600)
    return {
        "name": item["name"],
        "version": item["version"],
        "filename": item["filename"],
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "path": str(path),
    }


def cmd_speech_prepare(_: argparse.Namespace) -> None:
    """Prepares pinned SenseVoice assets and an isolated CPU-only Python runtime."""
    _require_wsl2_x64()
    _ensure_state()
    if sys.version_info[:2] != (3, 12):
        raise AcceptanceError("Speech acceptance requires the local Python 3.12 interpreter.")
    artifacts = [_prepare_model_artifact(item) for item in SPEECH_ARTIFACTS]
    wheels = [_prepare_speech_wheel(item) for item in SPEECH_WHEELS]
    site_packages = _speech_site_packages()
    python = Path(sys.executable)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--target",
            str(site_packages),
            "--no-index",
            "--no-deps",
            *(str(_speech_wheel_path(item)) for item in SPEECH_WHEELS),
        ],
        timeout=900,
        label="Pinned local speech package extraction",
    )
    package_env = {
        **os.environ,
        "PYTHONPATH": str(site_packages),
        "PYTHONNOUSERSITE": "1",
    }
    package_check = _run(
        [
            str(python),
            "-c",
            (
                "import grpc, numpy, google.protobuf, sherpa_onnx; "
                "from imageio_ffmpeg import get_ffmpeg_exe; "
                "import importlib.metadata as m; "
                "print('\\n'.join(f'{n}=={m.version(n)}' for n in "
                "('sherpa-onnx','sherpa-onnx-core','numpy','grpcio','protobuf','imageio-ffmpeg'))); "
                "print(get_ffmpeg_exe())"
            ),
        ],
        env=package_env,
        timeout=30,
        label="Pinned local speech runtime import check",
    )
    lines = package_check.stdout.strip().splitlines()
    if len(lines) != len(SPEECH_WHEELS) + 1:
        raise AcceptanceError("The task-local speech runtime returned unexpected package metadata.")
    ffmpeg_path = Path(lines[-1])
    if not ffmpeg_path.is_file() or not os.access(ffmpeg_path, os.X_OK):
        raise AcceptanceError("The task-local FFmpeg executable is unavailable.")
    ffmpeg_version = _run(
        [str(ffmpeg_path), "-version"], timeout=20, label="Task-local FFmpeg version check"
    ).stdout.splitlines()[0]
    ffmpeg_license = _run(
        [str(ffmpeg_path), "-L"], timeout=20, label="Task-local FFmpeg license check"
    ).stdout.splitlines()
    speech_manifest = {
        "prepared_at_utc": datetime.now(UTC).isoformat(),
        "source_policy": "immutable official Hugging Face revisions and PyPI wheel digests",
        "python": "3.12",
        "python_executable": str(python),
        "site_packages": str(site_packages),
        "runtime_packages": [line for line in lines[:-1]],
        "artifacts": artifacts,
        "wheels": wheels,
        "ffmpeg": {
            "path": str(ffmpeg_path),
            "sha256": _sha256(ffmpeg_path),
            "bytes": ffmpeg_path.stat().st_size,
            "version": ffmpeg_version,
            "license_report": [
                line.strip() for line in ffmpeg_license if "license" in line.lower()
            ][:4],
        },
        "model_terms": {
            "model": "FunASR Model Open Source License Agreement v1.1",
            "attribution": "Retain SenseVoice/FunASR author/source attribution and model name.",
            "source": "https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE",
        },
    }
    manifest_path = STATE_DIR / "models" / "speech" / "manifest.json"
    _write_private_file(manifest_path, json.dumps(speech_manifest, indent=2) + "\n")
    print(f"speech_assets=PREPARED artifacts={len(artifacts)} wheels={len(wheels)}")
    print(f"speech_runtime_python={python}")
    print(f"speech_ffmpeg={ffmpeg_version} sha256={speech_manifest['ffmpeg']['sha256']}")
    print(f"speech_manifest={manifest_path}")


def _git_head(path: Path, label: str) -> str:
    """Reads one candidate repository's exact local commit without changing it."""
    return _run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        timeout=15,
        label=f"{label} candidate revision lookup",
    ).stdout.strip()


def _source_revision(path: Path, label: str) -> str:
    """Reads an exact checkout SHA or a task-local immutable archive marker."""
    try:
        revision = _git_head(path, label)
    except AcceptanceError:
        marker = path / ".cyrene-source-commit"
        if not marker.is_file() or marker.is_symlink():
            raise
        revision = marker.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise AcceptanceError(f"The {label} source revision is not an exact commit SHA.")
    return revision


def cmd_speech_smoke(args: argparse.Namespace) -> None:
    """Runs the real local SenseVoice plugin process through DirectPluginRuntime."""
    _require_wsl2_x64()
    plugin_source = Path(args.plugin_source).resolve(strict=True)
    contracts_sdk = Path(args.contracts_sdk).resolve(strict=True)
    plugin_src = plugin_source / "src"
    runtime_src = contracts_sdk / "cyrene_plugin_runtime" / "src"
    speech_contracts_src = contracts_sdk / "cyrene_speech_provider_contracts" / "src"
    required = (
        plugin_src / "speech_sensevoice" / "plugin.py",
        runtime_src / "cyrene_plugin_runtime" / "bootstrap.py",
        speech_contracts_src / "cyrene_speech_provider_contracts" / "codec.py",
    )
    if not all(path.is_file() for path in required):
        raise AcceptanceError("The candidate plugin or Python contracts checkout is incomplete.")
    speech_dir = STATE_DIR / "models" / "speech"
    audio_path = speech_dir / "zh.wav"
    model_path = speech_dir / "model.int8.onnx"
    tokens_path = speech_dir / "tokens.txt"
    if not all(path.is_file() for path in (audio_path, model_path, tokens_path)):
        raise AcceptanceError(
            "Prepare the pinned speech assets first with the speech prepare command."
        )
    manifest_path = speech_dir / "manifest.json"
    try:
        manifest_info = manifest_path.lstat()
    except OSError:
        raise AcceptanceError(
            "Prepare the pinned speech runtime first with the speech prepare command."
        ) from None
    if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(manifest_info.st_mode):
        raise AcceptanceError("The prepared speech manifest must be a regular file.")
    for item in SPEECH_ARTIFACTS:
        artifact_path = _model_artifact_path(item)
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise AcceptanceError(f"The prepared speech artifact is unavailable: {item['name']}.")
        artifact_info = artifact_path.lstat()
        if (
            not stat.S_ISREG(artifact_info.st_mode)
            or artifact_info.st_uid != os.getuid()
            or artifact_info.st_size != item["size"]
            or _sha256(artifact_path) != item["sha256"]
        ):
            raise AcceptanceError(
                f"The prepared speech artifact failed integrity checks: {item['name']}."
            )
        expected_blob_sha1 = item.get("git_blob_sha1")
        if expected_blob_sha1 and _git_blob_sha1(artifact_path) != expected_blob_sha1:
            raise AcceptanceError(
                f"The prepared speech artifact source hash changed: {item['name']}."
            )
    speech_site_packages = _speech_site_packages()
    venv_python = Path(sys.executable)
    if not speech_site_packages.is_dir():
        raise AcceptanceError("The task-local speech Python runtime is not prepared.")
    plugin_sha = _source_revision(plugin_source, "Plugins")
    contracts_sha = _source_revision(contracts_sdk, "Contracts")
    if args.expected_plugin_sha and plugin_sha != args.expected_plugin_sha:
        raise AcceptanceError(
            "The Plugins speech candidate does not match the requested exact commit."
        )
    if args.expected_contracts_sha and contracts_sha != args.expected_contracts_sha:
        raise AcceptanceError(
            "The Contracts SDK candidate does not match the requested exact commit."
        )
    speech_manifest = _read_private_json(manifest_path, "speech manifest")
    ffmpeg_manifest = speech_manifest.get("ffmpeg")
    if not isinstance(ffmpeg_manifest, dict):
        raise AcceptanceError("The prepared speech manifest is missing FFmpeg metadata.")
    ffmpeg_path = Path(str(ffmpeg_manifest.get("path", "")))
    try:
        ffmpeg_relative = ffmpeg_path.relative_to(STATE_DIR)
        ffmpeg_info = ffmpeg_path.lstat()
    except (ValueError, OSError):
        raise AcceptanceError(
            "The prepared FFmpeg path is outside task state or unavailable."
        ) from None
    if (
        not ffmpeg_relative.parts
        or stat.S_ISLNK(ffmpeg_info.st_mode)
        or not stat.S_ISREG(ffmpeg_info.st_mode)
        or ffmpeg_info.st_uid != os.getuid()
        or ffmpeg_info.st_size != ffmpeg_manifest.get("bytes")
        or _sha256(ffmpeg_path) != ffmpeg_manifest.get("sha256")
    ):
        raise AcceptanceError("The prepared FFmpeg binary failed task-state integrity checks.")
    _secure_directory(STATE_DIR / "speech-home")
    provider_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(STATE_DIR / "speech-home"),
        "PYTHONPATH": ":".join(
            map(str, (speech_site_packages, plugin_src, runtime_src, speech_contracts_src))
        ),
        "PYTHONNOUSERSITE": "1",
        "CYRENE_CAPABILITY_BINDING_ID": "aether-wsl-speech-acceptance",
        "CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(
            {
                "model_path": str(model_path),
                "tokens_path": str(tokens_path),
                "model_id": "sensevoice-small-sherpa-onnx-int8-2024-07-17",
                "num_threads": 4,
                "max_input_bytes": 26214400,
                "max_duration_seconds": 300,
                "ffmpeg_path": speech_manifest["ffmpeg"]["path"],
            },
            separators=(",", ":"),
        ),
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    internal_runner = textwrap.dedent(
        """
        import json, os, subprocess, sys
        from pathlib import Path
        from cyrene_plugin_runtime.client import DirectPayload, DirectPluginClient
        from cyrene_speech_provider_contracts import (
            CAPABILITY_ID, INTERFACE_VERSION, METHOD_TRANSCRIBE,
            REQUEST_TYPE_URLS, RESPONSE_TYPE_URLS, decode_response, encode_request,
        )
        from cyrene_speech_provider_contracts import speech_provider_pb2 as wire
        plugin_root, runtime_bootstrap, audio_file = map(Path, sys.argv[1:4])
        proc = subprocess.Popen(
            [sys.executable, '-B', str(runtime_bootstrap), '--entrypoint',
             'speech_sensevoice.plugin:SenseVoiceSpeechProvider', '--capability',
             CAPABILITY_ID, '--interface-version', INTERFACE_VERSION,
             '--listen', '127.0.0.1:0'],
            cwd=plugin_root, env=os.environ.copy(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            line = proc.stdout.readline() if proc.stdout else ''
            if not line:
                raise RuntimeError('plugin_process_failed_before_ready')
            ready = json.loads(line)
            connection_ref = ready.get('connection_ref', '')
            if not connection_ref.startswith('grpc://127.0.0.1:'):
                raise RuntimeError('plugin_runtime_not_loopback')
            request = wire.TranscribeRequest()
            request.input.mime_type = 'audio/wav'
            request.input.data = audio_file.read_bytes()
            request.language_hint = 'zh'
            request.request_id = 'aether-wsl-asr-real-acceptance'
            encoded = encode_request(METHOD_TRANSCRIBE, request)
            with DirectPluginClient.for_local_connection_ref(connection_ref) as client:
                response_payload = client.invoke(
                    capability=CAPABILITY_ID,
                    interface_version=INTERFACE_VERSION,
                    method=METHOD_TRANSCRIBE,
                    request=DirectPayload(REQUEST_TYPE_URLS[METHOD_TRANSCRIBE], encoded),
                    deadline_seconds=300,
                    request_id='aether-wsl-asr-real-acceptance',
                )
            if response_payload.type_url != RESPONSE_TYPE_URLS[METHOD_TRANSCRIBE]:
                raise RuntimeError('plugin_runtime_response_type_mismatch')
            response = decode_response(METHOD_TRANSCRIBE, response_payload.value)
            if response.WhichOneof('result') != 'transcription':
                raise RuntimeError('plugin_returned_typed_error')
            result = response.transcription
            print(json.dumps({
                'status': 'PASS' if result.status == wire.TRANSCRIPTION_STATUS_SUCCEEDED else 'FAIL',
                'transcription_status': wire.TranscriptionStatus.Name(result.status),
                'text': result.text,
                'detected_language': result.detected_language,
                'duration_ms': result.duration_ms,
                'segment_count': len(result.segments),
                'model_id': result.model,
                'response_type_url': response_payload.type_url,
                'runtime_connection': 'loopback',
            }, ensure_ascii=False, separators=(',', ':')))
            if result.status != wire.TRANSCRIPTION_STATUS_SUCCEEDED or not result.text.strip():
                raise RuntimeError('plugin_returned_no_transcript')
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        """
    )
    result = _run(
        [
            str(venv_python),
            "-c",
            internal_runner,
            str(plugin_source),
            str(runtime_src / "cyrene_plugin_runtime" / "bootstrap.py"),
            str(audio_path),
        ],
        env=provider_env,
        cwd=plugin_source,
        timeout=420,
        label="Real SenseVoice DirectPluginRuntime acceptance",
    )
    try:
        provider_result = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise AcceptanceError("The real speech provider returned malformed evidence.") from None
    if provider_result.get("status") != "PASS":
        raise AcceptanceError("The real speech provider did not return a successful transcript.")
    expected_sample_text = "开放时间早上9点至下午5点。"
    actual_sample_text = str(provider_result.get("text", ""))
    receipt = {
        "status": "PASS",
        "classification": "REAL",
        "product_scope": "candidate SenseVoice plugin process via local DirectPluginRuntime; no Product route",
        "plugin_repository": "Plugins",
        "plugin_sha": plugin_sha,
        "contracts_sha": contracts_sha,
        "model_id": provider_result["model_id"],
        "model_sha256": _sha256(model_path),
        "tokens_sha256": _sha256(tokens_path),
        "audio_sha256": _sha256(audio_path),
        "audio_source_revision": "2365baeacb507f821a0c8120fcee3d484dba7a07",
        "sample_quality": {
            "upstream_expected_text": expected_sample_text,
            "exact_match": actual_sample_text == expected_sample_text,
            "review_note": "A one-character difference is recorded separately from provider execution status.",
        },
        "python_packages": speech_manifest["runtime_packages"],
        "ffmpeg": speech_manifest["ffmpeg"],
        "provider_result": provider_result,
        "completed_at_utc": datetime.now(UTC).isoformat(),
    }
    receipt_path = EVIDENCE_DIR / "speech-sensevoice-real.json"
    _write_private_file(receipt_path, json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    print(
        f"speech_provider=REAL status=PASS language={provider_result['detected_language']} "
        f"duration_ms={provider_result['duration_ms']} model={provider_result['model_id']}"
    )
    print(f"speech_receipt={receipt_path}")


def cmd_identity_prepare(_: argparse.Namespace) -> None:
    """Create local-only service credentials and a strict Navigator principal map."""
    _require_wsl2_x64()
    _ensure_state()
    model_receipt_path = EVIDENCE_DIR / "local-model-endpoints.json"
    model_receipt = _read_private_json(model_receipt_path, "local model receipt")
    if model_receipt.get("classification") != "REAL" or model_receipt.get("status") != "PASS":
        raise AcceptanceError("Prepare and verify the local Qwen/E5 model endpoints first.")

    def local_model_endpoint(name: str, expected_port: int) -> str:
        raw = model_receipt.get(name)
        if not isinstance(raw, str):
            raise AcceptanceError(f"The local model receipt is missing {name}.")
        parsed = urllib.parse.urlparse(raw)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError:
            raise AcceptanceError(f"The local model receipt has an invalid {name}.") from None
        if (
            parsed.scheme != "http"
            or address not in TASK_SUBNET
            or port != expected_port
            or parsed.path != "/v1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise AcceptanceError(f"The local model receipt has a non-task {name} endpoint.")
        return raw

    chat_url = local_model_endpoint("wsl_host_chat_endpoint", 8080)
    embeddings_url = local_model_endpoint("wsl_host_embedding_endpoint", 8081)
    model_manifest = _read_private_json(
        STATE_DIR / "models" / "manifest.json", "local model manifest"
    )
    expected_e5 = next(
        item
        for item in MODEL_ARTIFACTS
        if item["model_id"] == "intfloat/multilingual-e5-small"
        and item["relative_path"] == "onnx/model_qint8_avx512_vnni.onnx"
    )
    artifacts = model_manifest.get("artifacts")
    e5_artifact = (
        next(
            (
                item
                for item in artifacts
                if isinstance(item, dict)
                and item.get("model_id") == "intfloat/multilingual-e5-small"
                and item.get("sha256") == expected_e5["sha256"]
            ),
            None,
        )
        if isinstance(artifacts, list)
        else None
    )
    if (
        not isinstance(e5_artifact, dict)
        or e5_artifact.get("revision") != expected_e5["revision"]
        or e5_artifact.get("sha256") != expected_e5["sha256"]
    ):
        raise AcceptanceError(
            "The local E5 model manifest differs from the pinned embedding profile."
        )
    embedding_profile_fields = {
        "model_id": "intfloat/multilingual-e5-small",
        "model_revision": expected_e5["revision"],
        "model_sha256": expected_e5["sha256"],
        "dimensions": 384,
        "document_prefix": "passage: ",
        "query_prefix": "query: ",
    }
    embedding_profile_digest = hashlib.sha256(
        json.dumps(embedding_profile_fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    embedding_profile_id = f"sha256:{embedding_profile_digest}"
    product_state = STATE_DIR / "local-product"
    _secure_directory(product_state)
    environment_path = product_state / "services.env"
    principals_path = product_state / "navigator-principals.json"
    embedding_profile_path = product_state / "embedding-profile.json"
    identity_path = EVIDENCE_DIR / "local-service-identity.json"
    existing = tuple(
        path.exists() or path.is_symlink() for path in (environment_path, principals_path)
    )
    if any(existing) and not all(existing):
        raise AcceptanceError(
            "Local service identity files are incomplete; preserve them for review."
        )
    if all(existing):
        environment = _read_private_env(environment_path, "local service environment")
        principals = _read_private_json(principals_path, "Navigator principal configuration")
        rows = principals.get("principals")
        principal_actor_ids = (
            [principal.get("actor_id") for principal in rows if isinstance(principal, dict)]
            if isinstance(rows, list)
            else []
        )
        migration_actor_ids = {LEGACY_LOCAL_OWNER_ACTOR_ID, LOCAL_OWNER_ACTOR_ID}
        if (
            environment.get("CYRENE_EXCHANGE_ACTOR_ID") in migration_actor_ids
            and environment.get("CYRENE_WORKSPACE_OWNER_ACTOR_ID") in {None, *migration_actor_ids}
            and all(actor_id in migration_actor_ids for actor_id in principal_actor_ids)
            and (
                environment.get("CYRENE_EXCHANGE_ACTOR_ID") != LOCAL_OWNER_ACTOR_ID
                or environment.get("CYRENE_WORKSPACE_OWNER_ACTOR_ID") != LOCAL_OWNER_ACTOR_ID
                or LEGACY_LOCAL_OWNER_ACTOR_ID in principal_actor_ids
            )
        ):
            migrated_environment = dict(environment)
            migrated_environment["CYRENE_EXCHANGE_ACTOR_ID"] = LOCAL_OWNER_ACTOR_ID
            migrated_environment["CYRENE_WORKSPACE_OWNER_ACTOR_ID"] = LOCAL_OWNER_ACTOR_ID
            migrated_principals = json.loads(json.dumps(principals))
            principal_rows = migrated_principals.get("principals")
            if isinstance(principal_rows, list):
                for principal in principal_rows:
                    if (
                        isinstance(principal, dict)
                        and principal.get("actor_id") == LEGACY_LOCAL_OWNER_ACTOR_ID
                    ):
                        principal["actor_id"] = LOCAL_OWNER_ACTOR_ID
            _validate_local_identity(
                migrated_environment, migrated_principals, chat_url, embeddings_url
            )
            _write_private_file(
                environment_path,
                "".join(f"{name}={value}\n" for name, value in migrated_environment.items()),
            )
            _write_private_file(
                principals_path,
                json.dumps(migrated_principals, separators=(",", ":")) + "\n",
            )
            environment = migrated_environment
            principals = migrated_principals
        else:
            _validate_local_identity(environment, principals, chat_url, embeddings_url)
    else:
        data_token = secrets.token_hex(32)
        work_token = secrets.token_hex(32)
        session_token = secrets.token_hex(32)
        control_token = secrets.token_hex(32)
        bootstrap_token = secrets.token_hex(32)
        local_control_token = secrets.token_hex(32)
        model_id = "cyrene-local-qwen25-05b"
        environment = {
            "CYRENE_EXCHANGE_BEARER_TOKEN": data_token,
            "CYRENE_EXCHANGE_TOKEN": data_token,
            "CYRENE_EXCHANGE_CONTROL_BEARER_TOKEN": control_token,
            "CYRENE_EXCHANGE_BOOTSTRAP_BEARER_TOKEN": bootstrap_token,
            "CYRENE_EXCHANGE_URL": "http://127.0.0.1:18682/v1",
            "CYRENE_EXCHANGE_ACTOR_ID": LOCAL_OWNER_ACTOR_ID,
            "CYRENE_WORKSPACE_OWNER_ACTOR_ID": LOCAL_OWNER_ACTOR_ID,
            "CYRENE_EXCHANGE_ORGANIZATION_ID": "cyrene-aether-local",
            "CYRENE_EXCHANGE_WORKSPACE_ID": "local",
            "CYRENE_EXCHANGE_CREDENTIAL_REF": "cred://cyrene/aether-wsl-product-data",
            "CYRENE_EXCHANGE_CONTROL_CREDENTIAL_REF": "cred://cyrene/aether-wsl-product-control",
            "CYRENE_EXCHANGE_BOOTSTRAP_CREDENTIAL_REF": "cred://cyrene/aether-wsl-product-bootstrap",
            "CYRENE_HARNESS_MODEL": model_id,
            "CYRENE_HARNESS_MODEL_SOURCE": "exchange",
            "CYRENE_VLLM_BASE_URL": chat_url,
            "CYRENE_VLLM_MODEL": model_id,
            "CYRENE_CHAT_BASE_URL": chat_url,
            "CYRENE_CHAT_MODEL": model_id,
            "CYRENE_CHAT_API_KEY": "",
            "CYRENE_CHAT_TIMEOUT": "90",
            "CYRENE_EMBEDDINGS_SUPPORTED": "true",
            "CYRENE_EMBEDDINGS_BASE_URL": embeddings_url,
            "CYRENE_EMBEDDINGS_MODEL": "intfloat/multilingual-e5-small",
            "CYRENE_EMBEDDINGS_TIMEOUT": "30",
            "CYRENE_SESSION_TOKEN": session_token,
            "CYRENE_WORK_ATTACHMENT_TOKEN": work_token,
            "CYRENE_WORK_ATTACHMENT_ORIGIN": "http://127.0.0.1:18681",
            "CYRENE_WORK_ATTACHMENT_CREDENTIAL_REF": "cred://cyrene/aether-wsl-work-attachments",
            "STUDIO_LOCAL_API_TOKEN": local_control_token,
            "STUDIO_WORKSPACE_ID": "local",
            "CYRENE_WORKSPACE_ID": "local",
            "CYRENE_ORGANIZATION_ID": "cyrene-aether-local",
        }
        principals = {
            "principals": [
                {
                    "token_env": "CYRENE_SESSION_TOKEN",
                    "actor_id": LOCAL_OWNER_ACTOR_ID,
                    "workspace_ids": ["local"],
                    "organization_id": "cyrene-aether-local",
                    "can_takeover": True,
                    "can_write_harness": True,
                    "actor_private_scope": False,
                },
                {
                    "token_env": "CYRENE_WORK_ATTACHMENT_TOKEN",
                    "actor_id": LOCAL_OWNER_ACTOR_ID,
                    "workspace_ids": ["local"],
                    "organization_id": "cyrene-aether-local",
                    "actor_private_scope": True,
                },
            ]
        }
        _write_private_file(
            environment_path,
            "".join(f"{name}={value}\n" for name, value in environment.items()),
        )
        _write_private_file(principals_path, json.dumps(principals, separators=(",", ":")) + "\n")
        _validate_local_identity(environment, principals, chat_url, embeddings_url)

    _write_private_file(
        embedding_profile_path,
        json.dumps({"profile_id": embedding_profile_id, **embedding_profile_fields}, indent=2)
        + "\n",
    )

    identity = {
        "status": "PREPARED",
        "classification": "LOCAL_STATIC_BEARER_CONFIGURATION",
        "identity": {
            "actor_id": LOCAL_OWNER_ACTOR_ID,
            "organization_id": "cyrene-aether-local",
            "workspace_id": "local",
        },
        "service_endpoints": {
            "navigator": "http://127.0.0.1:18680",
            "persistence": "http://127.0.0.1:18681",
            "exchange": "http://127.0.0.1:18682/v1",
            "client": "http://127.0.0.1:18684",
            "control": "http://127.0.0.1:18685",
        },
        "model_endpoints": {"chat": chat_url, "embeddings": embeddings_url},
        "embedding_profile_id": embedding_profile_id,
        "private_files": {
            "environment": str(environment_path),
            "navigator_principals": str(principals_path),
            "embedding_profile": str(embedding_profile_path),
        },
        "secret_environment_names": sorted(
            name for name in environment if name.endswith(("TOKEN", "BEARER_TOKEN", "API_KEY"))
        ),
        "external_directory_or_oidc": "NOT_CONFIGURED",
        "prepared_at_utc": datetime.now(UTC).isoformat(),
    }
    _write_private_file(identity_path, json.dumps(identity, indent=2) + "\n")
    print("local_identity=PREPARED auth=static-local-bearer external_directory=NOT_CONFIGURED")
    print(f"local_service_environment={environment_path}")
    print(f"navigator_principals={principals_path}")
    print(f"local_identity_receipt={identity_path}")


def _validate_local_identity(
    environment: dict[str, str],
    principal_config: dict[str, Any],
    expected_chat_url: str,
    expected_embeddings_url: str,
) -> None:
    """Fail closed unless private local identity and model bindings match this task."""
    expected_environment_names = {
        "CYRENE_EXCHANGE_BEARER_TOKEN",
        "CYRENE_EXCHANGE_TOKEN",
        "CYRENE_EXCHANGE_CONTROL_BEARER_TOKEN",
        "CYRENE_EXCHANGE_BOOTSTRAP_BEARER_TOKEN",
        "CYRENE_EXCHANGE_URL",
        "CYRENE_EXCHANGE_ACTOR_ID",
        "CYRENE_WORKSPACE_OWNER_ACTOR_ID",
        "CYRENE_EXCHANGE_ORGANIZATION_ID",
        "CYRENE_EXCHANGE_WORKSPACE_ID",
        "CYRENE_EXCHANGE_CREDENTIAL_REF",
        "CYRENE_EXCHANGE_CONTROL_CREDENTIAL_REF",
        "CYRENE_EXCHANGE_BOOTSTRAP_CREDENTIAL_REF",
        "CYRENE_HARNESS_MODEL",
        "CYRENE_HARNESS_MODEL_SOURCE",
        "CYRENE_VLLM_BASE_URL",
        "CYRENE_VLLM_MODEL",
        "CYRENE_CHAT_BASE_URL",
        "CYRENE_CHAT_MODEL",
        "CYRENE_CHAT_API_KEY",
        "CYRENE_CHAT_TIMEOUT",
        "CYRENE_EMBEDDINGS_SUPPORTED",
        "CYRENE_EMBEDDINGS_BASE_URL",
        "CYRENE_EMBEDDINGS_MODEL",
        "CYRENE_EMBEDDINGS_TIMEOUT",
        "CYRENE_SESSION_TOKEN",
        "CYRENE_WORK_ATTACHMENT_TOKEN",
        "CYRENE_WORK_ATTACHMENT_ORIGIN",
        "CYRENE_WORK_ATTACHMENT_CREDENTIAL_REF",
        "STUDIO_LOCAL_API_TOKEN",
        "STUDIO_WORKSPACE_ID",
        "CYRENE_WORKSPACE_ID",
        "CYRENE_ORGANIZATION_ID",
    }
    if set(environment) != expected_environment_names:
        raise AcceptanceError(
            "The private local service environment contains extra or missing keys."
        )
    required_values = {
        "CYRENE_EXCHANGE_URL": "http://127.0.0.1:18682/v1",
        "CYRENE_EXCHANGE_ACTOR_ID": LOCAL_OWNER_ACTOR_ID,
        "CYRENE_WORKSPACE_OWNER_ACTOR_ID": LOCAL_OWNER_ACTOR_ID,
        "CYRENE_EXCHANGE_ORGANIZATION_ID": "cyrene-aether-local",
        "CYRENE_EXCHANGE_WORKSPACE_ID": "local",
        "CYRENE_EXCHANGE_CREDENTIAL_REF": "cred://cyrene/aether-wsl-product-data",
        "CYRENE_EXCHANGE_CONTROL_CREDENTIAL_REF": "cred://cyrene/aether-wsl-product-control",
        "CYRENE_EXCHANGE_BOOTSTRAP_CREDENTIAL_REF": "cred://cyrene/aether-wsl-product-bootstrap",
        "CYRENE_HARNESS_MODEL": "cyrene-local-qwen25-05b",
        "CYRENE_HARNESS_MODEL_SOURCE": "exchange",
        "CYRENE_VLLM_BASE_URL": expected_chat_url,
        "CYRENE_VLLM_MODEL": "cyrene-local-qwen25-05b",
        "CYRENE_CHAT_BASE_URL": expected_chat_url,
        "CYRENE_CHAT_MODEL": "cyrene-local-qwen25-05b",
        "CYRENE_CHAT_API_KEY": "",
        "CYRENE_CHAT_TIMEOUT": "90",
        "CYRENE_EMBEDDINGS_SUPPORTED": "true",
        "CYRENE_EMBEDDINGS_BASE_URL": expected_embeddings_url,
        "CYRENE_EMBEDDINGS_MODEL": "intfloat/multilingual-e5-small",
        "CYRENE_EMBEDDINGS_TIMEOUT": "30",
        "CYRENE_WORK_ATTACHMENT_ORIGIN": "http://127.0.0.1:18681",
        "CYRENE_WORK_ATTACHMENT_CREDENTIAL_REF": "cred://cyrene/aether-wsl-work-attachments",
        "STUDIO_WORKSPACE_ID": "local",
        "CYRENE_WORKSPACE_ID": "local",
        "CYRENE_ORGANIZATION_ID": "cyrene-aether-local",
    }
    if any(environment.get(name) != value for name, value in required_values.items()):
        raise AcceptanceError(
            "The private local service environment differs from the fixed profile."
        )
    try:
        if str(UUID(LOCAL_OWNER_ACTOR_ID)) != LOCAL_OWNER_ACTOR_ID:
            raise ValueError
    except ValueError:
        raise AcceptanceError("The fixed local owner actor id must be a UUID.") from None
    for name in (
        "CYRENE_EXCHANGE_BEARER_TOKEN",
        "CYRENE_EXCHANGE_TOKEN",
        "CYRENE_EXCHANGE_CONTROL_BEARER_TOKEN",
        "CYRENE_EXCHANGE_BOOTSTRAP_BEARER_TOKEN",
        "CYRENE_SESSION_TOKEN",
        "CYRENE_WORK_ATTACHMENT_TOKEN",
        "STUDIO_LOCAL_API_TOKEN",
    ):
        value = environment.get(name, "")
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise AcceptanceError(
                "The private local service environment is missing a generated token."
            )
    if environment["CYRENE_EXCHANGE_BEARER_TOKEN"] != environment["CYRENE_EXCHANGE_TOKEN"]:
        raise AcceptanceError(
            "Navigator and Product must use the same local data-plane credential."
        )
    role_tokens = [
        environment[name]
        for name in (
            "CYRENE_EXCHANGE_BEARER_TOKEN",
            "CYRENE_EXCHANGE_CONTROL_BEARER_TOKEN",
            "CYRENE_EXCHANGE_BOOTSTRAP_BEARER_TOKEN",
            "CYRENE_SESSION_TOKEN",
            "CYRENE_WORK_ATTACHMENT_TOKEN",
            "STUDIO_LOCAL_API_TOKEN",
        )
    ]
    if len(role_tokens) != len(set(role_tokens)):
        raise AcceptanceError("Local service roles must use distinct bearer credentials.")
    if set(principal_config) != {"principals"}:
        raise AcceptanceError("The private Navigator principal map contains unsupported fields.")
    rows = principal_config.get("principals")
    if (
        not isinstance(rows, list)
        or len(rows) != 2
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise AcceptanceError("The private Navigator principal map is malformed.")
    by_env = {row.get("token_env"): row for row in rows}
    owner = by_env.get("CYRENE_SESSION_TOKEN", {})
    attachment = by_env.get("CYRENE_WORK_ATTACHMENT_TOKEN", {})
    owner_fields = {
        "token_env",
        "actor_id",
        "workspace_ids",
        "organization_id",
        "can_takeover",
        "can_write_harness",
        "actor_private_scope",
    }
    attachment_fields = {
        "token_env",
        "actor_id",
        "workspace_ids",
        "organization_id",
        "actor_private_scope",
    }
    if set(owner) != owner_fields or set(attachment) != attachment_fields:
        raise AcceptanceError("The private Navigator principal map contains unsupported grants.")
    expected_scope = {
        "actor_id": LOCAL_OWNER_ACTOR_ID,
        "workspace_ids": ["local"],
        "organization_id": "cyrene-aether-local",
    }
    if (
        owner.get("token_env") != "CYRENE_SESSION_TOKEN"
        or any(owner.get(name) != value for name, value in expected_scope.items())
        or owner.get("actor_private_scope") is not False
    ):
        raise AcceptanceError("The private Navigator owner principal has an unexpected scope.")
    if owner.get("can_takeover") is not True or owner.get("can_write_harness") is not True:
        raise AcceptanceError("The private Navigator owner principal is missing local Work grants.")
    if attachment.get("token_env") != "CYRENE_WORK_ATTACHMENT_TOKEN" or any(
        attachment.get(name) != value for name, value in expected_scope.items()
    ):
        raise AcceptanceError("The private attachment principal has an unexpected scope.")
    if (
        any(
            attachment.get(name) is True
            for name in ("can_takeover", "can_write_harness", "can_publish_credential_health")
        )
        or attachment.get("actor_private_scope") is not True
    ):
        raise AcceptanceError("The private attachment principal is not read-only actor-scoped.")


def _task_service_container(service: str) -> str:
    """Finds exactly one running container for a task-owned Compose service."""
    matches = _run(
        [
            "docker",
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={PROJECT_NAME}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        timeout=15,
        label=f"Acceptance service lookup for {service}",
    ).stdout.split()
    if len(matches) != 1:
        raise AcceptanceError(f"The task {service} service is not running exactly once.")
    return matches[0]


def _verify_model_service(container_id: str, service: str) -> str:
    """Checks task labels, private-network membership, and the absence of host port mappings."""
    details = json.loads(
        _run(
            ["docker", "inspect", "--format", "{{json .}}", container_id],
            timeout=10,
            label=f"Acceptance service isolation check for {service}",
        ).stdout
    )
    labels = details.get("Config", {}).get("Labels") or {}
    if (
        labels.get(TASK_LABEL) != TASK_LABEL_VALUE
        or labels.get("com.docker.compose.service") != service
    ):
        raise AcceptanceError(f"The task {service} service is missing its ownership labels.")
    bindings = details.get("HostConfig", {}).get("PortBindings") or {}
    if any(binding for binding in bindings.values()):
        raise AcceptanceError(f"The task {service} service unexpectedly publishes a host port.")
    networks = details.get("NetworkSettings", {}).get("Networks", {})
    if list(networks) != [f"{PROJECT_NAME}_data"]:
        raise AcceptanceError(
            f"The task {service} service is attached to an unexpected Docker network."
        )
    address = next(iter(networks.values())).get("IPAddress", "")
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise AcceptanceError(
            f"The task {service} service has no private network address."
        ) from None
    return address


def _http_json(
    url: str, payload: dict[str, Any] | None = None, *, timeout: int = 10
) -> dict[str, Any]:
    """Makes one loopback-only JSON request and returns a decoded response object."""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(8 * 1024 * 1024))


def _wait_for_local_models(qwen_address: str, embedding_address: str) -> dict[str, Any]:
    """Waits for local model APIs, then exercises real chat and embedding inference."""
    deadline = time.monotonic() + 240
    qwen_models: dict[str, Any] | None = None
    embedding_health: dict[str, Any] | None = None
    qwen_base = f"http://{qwen_address}:8080/v1"
    embedding_base = f"http://{embedding_address}:8081/v1"
    while time.monotonic() < deadline:
        try:
            qwen_models = _http_json(f"{qwen_base}/models", timeout=5)
            embedding_health = _http_json(f"{embedding_base[:-3]}/health", timeout=5)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(3)
    if qwen_models is None or embedding_health is None:
        raise AcceptanceError(
            "The local chat/embedding model endpoints did not become ready within four minutes."
        )
    qwen_ids = {entry.get("id") for entry in qwen_models.get("data", []) if isinstance(entry, dict)}
    if "cyrene-local-qwen25-05b" not in qwen_ids:
        raise AcceptanceError("The local Qwen endpoint did not expose the expected model alias.")
    if embedding_health.get("status") != "ok" or embedding_health.get("dimensions") != 384:
        raise AcceptanceError("The local E5 endpoint returned unexpected model readiness metadata.")

    chat = _http_json(
        f"{qwen_base}/chat/completions",
        {
            "model": "cyrene-local-qwen25-05b",
            "messages": [{"role": "user", "content": "Reply with the exact word: READY"}],
            "temperature": 0,
            "max_tokens": 8,
            "stream": False,
        },
        timeout=90,
    )
    chat_content = chat.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(chat_content, str) or not chat_content.strip():
        raise AcceptanceError("The local Qwen endpoint returned no generated response content.")

    embedding = _http_json(
        f"{embedding_base}/embeddings",
        {
            "model": "intfloat/multilingual-e5-small",
            "input": [
                "query: 哪些长期记忆属于本地验收？",
                "passage: 本地验收记忆保留来源和授权范围。",
            ],
        },
        timeout=30,
    )
    entries = embedding.get("data", [])
    if len(entries) != 2 or sorted(item.get("index") for item in entries) != [0, 1]:
        raise AcceptanceError("The local E5 endpoint did not return a complete indexed batch.")
    dimensions = {len(item.get("embedding", [])) for item in entries}
    if (
        dimensions != {384}
        or not isinstance(embedding.get("model"), str)
        or not embedding.get("model")
    ):
        raise AcceptanceError(
            "The local E5 endpoint returned an unexpected model or vector dimension."
        )
    for item in entries:
        vector = item["embedding"]
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise AcceptanceError("The local E5 endpoint returned a non-finite embedding.")
        norm = math.sqrt(sum(float(value) * float(value) for value in vector))
        if norm < 0.99 or norm > 1.01:
            raise AcceptanceError(
                "The local E5 endpoint returned a non-finite, zero, or unnormalized vector."
            )
    return {
        "status": "PASS",
        "classification": "REAL",
        "qwen_model": "cyrene-local-qwen25-05b",
        "qwen_nonempty_response": True,
        "e5_model": embedding["model"],
        "e5_dimension": 384,
        "e5_vectors": 2,
        "e5_vectors_normalized": True,
        "wsl_host_chat_endpoint": qwen_base,
        "wsl_host_embedding_endpoint": embedding_base,
        "completed_at_utc": datetime.now(UTC).isoformat(),
    }


def cmd_models_up(_: argparse.Namespace) -> None:
    """Builds and starts task-local CPU models, then proves real local inference."""
    _require_wsl2_x64()
    _require_local_docker()
    _verify_task_subnet_available()
    missing = []
    for item in MODEL_ARTIFACTS:
        path = _model_artifact_path(item)
        if (
            not path.is_file()
            or path.stat().st_size != item["size"]
            or _sha256(path) != item["sha256"]
        ):
            missing.append(item["name"])
    if missing:
        raise AcceptanceError("Prepare pinned local models first: run the models prepare command.")
    _verify_project_ownership()
    _compose("--profile", "models", "up", "--detach", "qwen", "embedding", timeout=1800)
    qwen_id = _task_service_container("qwen")
    embedding_id = _task_service_container("embedding")
    qwen_address = _verify_model_service(qwen_id, "qwen")
    embedding_address = _verify_model_service(embedding_id, "embedding")
    receipt = _wait_for_local_models(qwen_address, embedding_address)
    receipt_path = EVIDENCE_DIR / "local-model-endpoints.json"
    temporary_receipt = EVIDENCE_DIR / ".local-model-endpoints.json.tmp"
    _secure_directory(EVIDENCE_DIR)
    temporary_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary_receipt, 0o600)
    os.replace(temporary_receipt, receipt_path)
    host_env = (
        f"export CYRENE_CHAT_BASE_URL={receipt['wsl_host_chat_endpoint']}\n"
        "export CYRENE_CHAT_TIMEOUT=90\n"
        "export CYRENE_EMBEDDINGS_SUPPORTED=true\n"
        f"export CYRENE_EMBEDDINGS_BASE_URL={receipt['wsl_host_embedding_endpoint']}\n"
        "export CYRENE_EMBEDDINGS_TIMEOUT=30\n"
        "export CYRENE_EMBEDDINGS_MODEL=intfloat/multilingual-e5-small\n"
    )
    container_env = (
        "export CYRENE_CHAT_BASE_URL=http://aether-qwen:8080/v1\n"
        "export CYRENE_CHAT_TIMEOUT=90\n"
        "export CYRENE_EMBEDDINGS_SUPPORTED=true\n"
        "export CYRENE_EMBEDDINGS_BASE_URL=http://aether-embedding:8081/v1\n"
        "export CYRENE_EMBEDDINGS_TIMEOUT=30\n"
        "export CYRENE_EMBEDDINGS_MODEL=intfloat/multilingual-e5-small\n"
    )
    _write_private_file(EVIDENCE_DIR / "models-host.env", host_env)
    _write_private_file(EVIDENCE_DIR / "models-container.env", container_env)
    print(
        f"qwen_chat=REAL inference=PASS endpoint={receipt['wsl_host_chat_endpoint']} model=cyrene-local-qwen25-05b"
    )
    print(
        f"e5_embeddings=REAL inference=PASS endpoint={receipt['wsl_host_embedding_endpoint']} model=intfloat/multilingual-e5-small dimensions=384"
    )
    print(
        "container_dns=aether-qwen:8080,aether-embedding:8081 network=internal host_ports_published=false"
    )
    print(
        f"host_process_env={STATE_DIR / 'evidence/models-host.env'} container_env={STATE_DIR / 'evidence/models-container.env'}"
    )
    print(f"model_smoke_receipt={receipt_path}")


def cmd_models_status(_: argparse.Namespace) -> None:
    """Reports whether the task-local chat and embedding services are running."""
    for service in ("qwen", "embedding"):
        try:
            container_id = _task_service_container(service)
            address = _verify_model_service(container_id, service)
            state = _run(
                ["docker", "inspect", "--format", "{{.State.Status}}", container_id],
                timeout=10,
                label=f"Acceptance model status for {service}",
            ).stdout.strip()
            port = 8080 if service == "qwen" else 8081
            print(f"service={service} state={state} private_address={address}:{port}")
        except AcceptanceError:
            print(f"service={service} state=absent")


def _browser_binary(requested: str | None) -> Path:
    """Resolves the Playwright Chromium executable without downloading a browser."""
    if requested:
        browser = Path(requested).expanduser().resolve(strict=True)
        if not browser.is_file():
            raise AcceptanceError("The requested browser path is not a regular executable file.")
        return browser
    cache = Path.home() / ".cache/ms-playwright"
    candidates = sorted(cache.glob("chromium-*/chrome-linux64/chrome"), reverse=True)
    candidates.extend(sorted(cache.glob("chromium-*/chrome-linux/chrome"), reverse=True))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise AcceptanceError(
        "No installed Playwright Chromium binary was found; this command does not download one."
    )


def _apt_metadata(package: str, version: str, expected_sha256: str) -> str:
    """Confirms the cached Ubuntu package index contains the pinned artifact digest."""
    result = _run(
        ["apt-cache", "show", f"{package}={version}"],
        timeout=15,
        label=f"Ubuntu package metadata check for {package}",
    )
    for record in result.stdout.split("\n\n"):
        values = {}
        for line in record.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key] = value.strip()
        if values.get("Package") == package and values.get("Version") == version:
            filename = values.get("Filename", "")
            digest = values.get("SHA256", "")
            if digest != expected_sha256 or not filename.startswith("pool/"):
                raise AcceptanceError(
                    f"Ubuntu package metadata changed for {package}; stop for review."
                )
            return filename.rsplit("/", 1)[-1]
    raise AcceptanceError(f"The pinned Ubuntu package version is not available for {package}.")


def _browser_environment(library_root: Path) -> dict[str, str]:
    """Builds private dynamic-library and ALSA paths for the browser process."""
    library_dirs = [
        library_root / "usr/lib/x86_64-linux-gnu/nss",
        library_root / "usr/lib/x86_64-linux-gnu",
    ]
    library_path = ":".join(str(path) for path in library_dirs if path.is_dir())
    env = os.environ.copy()
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = ":".join(part for part in (library_path, existing) if part)
    alsa_config = library_root / "usr/share/alsa/alsa.conf"
    alsa_plugins = library_root / "usr/lib/x86_64-linux-gnu/alsa-lib"
    if alsa_config.is_file():
        env["ALSA_CONFIG_PATH"] = str(alsa_config)
    if alsa_plugins.is_dir():
        env["ALSA_PLUGIN_DIR"] = str(alsa_plugins)
    return env


def cmd_browser_deps(args: argparse.Namespace) -> None:
    """Downloads, hashes, and privately extracts only Playwright's missing Ubuntu libraries."""
    _require_wsl2_x64()
    _ensure_state()
    browser = _browser_binary(args.browser)
    library_root = TOOLCHAIN_DIR / "browser-libs"
    download_root = TOOLCHAIN_DIR / "browser-debs"
    _secure_directory(library_root)
    _secure_directory(download_root)
    manifest_packages = []
    for package, version, expected_sha in BROWSER_PACKAGES:
        filename = _apt_metadata(package, version, expected_sha)
        cached = sorted(
            candidate
            for candidate in download_root.glob("*.deb")
            if candidate.is_file() and _sha256(candidate) == expected_sha
        )
        artifact = cached[0] if cached else download_root / filename
        if not cached:
            with tempfile.TemporaryDirectory(prefix=f"{package}-", dir=download_root) as temp_name:
                temp_root = Path(temp_name)
                _run(
                    ["apt-get", "download", f"{package}={version}"],
                    cwd=temp_root,
                    timeout=120,
                    label=f"Ubuntu package download for {package}",
                )
                downloaded_archives = list(temp_root.glob("*.deb"))
                if len(downloaded_archives) != 1 or _sha256(downloaded_archives[0]) != expected_sha:
                    raise AcceptanceError(
                        f"The Ubuntu package failed its pinned SHA-256 check: {package}."
                    )
                downloaded = downloaded_archives[0]
                if artifact.exists():
                    raise AcceptanceError(
                        f"A conflicting task-local Ubuntu archive already exists: {package}."
                    )
                os.replace(downloaded, artifact)
        if _sha256(artifact) != expected_sha:
            raise AcceptanceError(
                f"The task-local Ubuntu archive failed its SHA-256 check: {package}."
            )
        _run(
            ["dpkg-deb", "--extract", str(artifact), str(library_root)],
            timeout=30,
            label=f"Private package extraction for {package}",
        )
        manifest_packages.append(
            {"name": package, "version": version, "filename": filename, "sha256": expected_sha}
        )

    env = _browser_environment(library_root)
    dependencies = _run(
        ["ldd", str(browser)], env=env, timeout=20, label="Playwright browser dependency check"
    )
    missing = sorted(
        {
            line.strip().split()[0]
            for line in dependencies.stdout.splitlines()
            if "not found" in line
        }
    )
    if missing:
        raise AcceptanceError(
            "Playwright browser still lacks shared libraries: " + ", ".join(missing)
        )

    manifest = {
        "browser_path": str(browser),
        "packages": manifest_packages,
        "extracted_to": str(library_root),
        "system_package_database_modified": False,
        "system_library_paths_modified": False,
        "verified_ldd_missing_count": 0,
        "verified_at_utc": datetime.now(UTC).isoformat(),
    }
    manifest_path = TOOLCHAIN_DIR / "browser-libs-manifest.json"
    temporary_manifest = TOOLCHAIN_DIR / ".browser-libs-manifest.json.tmp"
    temporary_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary_manifest, 0o600)
    os.replace(temporary_manifest, manifest_path)
    env_file = TOOLCHAIN_DIR / "browser-libs.env"
    lib_paths = env["LD_LIBRARY_PATH"].split(":")
    prefix = ":".join(path for path in lib_paths if path.startswith(str(library_root)))
    env_lines = [f'export LD_LIBRARY_PATH="{prefix}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"']
    if "ALSA_CONFIG_PATH" in env:
        env_lines.append(f'export ALSA_CONFIG_PATH="{env["ALSA_CONFIG_PATH"]}"')
    if "ALSA_PLUGIN_DIR" in env:
        env_lines.append(f'export ALSA_PLUGIN_DIR="{env["ALSA_PLUGIN_DIR"]}"')
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    os.chmod(env_file, 0o600)
    _write_private_file(
        EVIDENCE_DIR / "browser-libraries.json", json.dumps(manifest, indent=2) + "\n"
    )

    launch_result = "NOT_RUN"
    if args.playwright_package:
        package = Path(args.playwright_package).expanduser().resolve(strict=True)
        if not (package / "package.json").is_file():
            raise AcceptanceError("The Playwright package path does not contain package.json.")
        node_binary = TOOLCHAIN_DIR / f"node-v{NODE_VERSION}-linux-x64/bin/node"
        if not node_binary.is_file():
            raise AcceptanceError(
                "Install the task-local Node toolchain before the Playwright smoke check."
            )
        javascript = (
            "const { chromium } = require(process.argv[1]); "
            '(async () => { const browser = await chromium.launch({headless:true,args:["--no-sandbox"]}); '
            "const page = await browser.newPage(); "
            'await page.goto("data:text/html,<title>cyrene-local-browser-ok</title>"); '
            "const title = await page.title(); await browser.close(); "
            'if (title !== "cyrene-local-browser-ok") process.exit(3); '
            'console.log("playwright_browser_smoke=PASS"); })()'
            ".catch(() => process.exit(1));"
        )
        with tempfile.TemporaryDirectory(
            prefix="aether-playwright-smoke-", dir=STATE_DIR
        ) as temp_dir:
            env["TMPDIR"] = temp_dir
            env["PATH"] = f"{node_binary.parent}:{env.get('PATH', '')}"
            smoke = _run(
                [str(node_binary), "-e", javascript, str(package)],
                env=env,
                timeout=45,
                label="Playwright Chromium launch smoke",
            )
            if "playwright_browser_smoke=PASS" not in smoke.stdout:
                raise AcceptanceError(
                    "Playwright Chromium did not report a completed local smoke check."
                )
        launch_result = "PASS"
    print(f"browser={browser}")
    print(
        f"browser_libraries=PASS playwright_launch={launch_result} private_libraries={library_root}"
    )
    print(f"browser_env_file={env_file} manifest={manifest_path}")


def cmd_up(_: argparse.Namespace) -> None:
    """Starts only the private, task-labelled pgvector database service."""
    _require_wsl2_x64()
    _require_local_docker()
    _verify_task_subnet_available()
    _verify_project_ownership()
    _ensure_postgres_env()
    _ensure_postgres_data_dir()
    _compose("up", "--detach", "postgres", timeout=600)
    result = _compose("ps", "--quiet", "postgres", timeout=30)
    container_id = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        raise AcceptanceError("Compose did not return the task PostgreSQL container identifier.")
    _verify_postgres_isolation(container_id)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = _run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
            timeout=10,
            label="Local PostgreSQL health check",
        ).stdout.strip()
        if status == "healthy":
            break
        if status == "unhealthy":
            raise AcceptanceError(
                "The isolated PostgreSQL container is unhealthy; data was retained for inspection."
            )
        time.sleep(2)
    else:
        raise AcceptanceError(
            "The isolated PostgreSQL container did not become healthy within 60 seconds."
        )
    readiness = _compose(
        "exec",
        "--no-TTY",
        "postgres",
        "pg_isready",
        "-U",
        "cyrene_aether",
        "-d",
        "cyrene_aether",
        timeout=20,
    )
    if "accepting connections" not in readiness.stdout.lower():
        raise AcceptanceError(
            "The isolated PostgreSQL readiness check did not confirm an accepting server."
        )
    vector_extension = _compose(
        "exec",
        "--no-TTY",
        "postgres",
        "psql",
        "-U",
        "cyrene_aether",
        "-d",
        "cyrene_aether",
        "-Atqc",
        "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'",
        timeout=20,
    ).stdout.strip()
    if not vector_extension:
        raise AcceptanceError(
            "The isolated PostgreSQL image does not provide the pgvector extension."
        )
    postgres_receipt = {
        "status": "PASS",
        "mode": "REAL",
        "project": PROJECT_NAME,
        "service": "postgres",
        "image": "pgvector/pgvector:pg17@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8",
        "network": {"subnet": str(TASK_SUBNET), "internal": True, "host_port_published": False},
        "pgvector_available_version": vector_extension,
        "database_state_dir": str(STATE_DIR / "postgres-data"),
        "completed_at_utc": datetime.now(UTC).isoformat(),
    }
    _write_private_file(
        EVIDENCE_DIR / "postgres-ready.json", json.dumps(postgres_receipt, indent=2) + "\n"
    )
    print(f"project={PROJECT_NAME} service=postgres state=healthy")
    print("network=project-private internal=true host_port_published=false")
    print(
        f"state_dir={STATE_DIR} database=cyrene_aether user=cyrene_aether password_file={DATABASE_ENV}"
    )
    print("container_network_address=postgres:5432")


def cmd_status(_: argparse.Namespace) -> None:
    """Shows state for task-labelled containers without reading container environments."""
    resources = _verify_project_ownership()
    if not resources:
        print(f"project={PROJECT_NAME} state=absent")
        return
    for resource in resources:
        print(
            f"container={resource.get('Names', 'unknown')} "
            f"state={resource.get('State', 'unknown')} status={resource.get('Status', 'unknown')} "
            f"task_label=verified"
        )
    print(f"database_state_dir={STATE_DIR / 'postgres-data'} retained_after_down=True")


def cmd_run_with_database(args: argparse.Namespace) -> int:
    """Runs a test command with a private PostgreSQL DSN injected and output redacted."""
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command or any(
        "postgres://" in part.lower() or "postgresql://" in part.lower() for part in command
    ):
        raise AcceptanceError(
            "Pass a test command after -- and do not place database URLs in command arguments."
        )
    cwd = Path(args.cwd).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise AcceptanceError("The requested test working directory is not a directory.")
    dsn, password = _postgres_connection()
    env = os.environ.copy()
    env["CYRENE_TEST_POSTGRES_DSN"] = dsn
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
        raw_output = result.stdout + result.stderr
        exit_code = result.returncode
    except subprocess.TimeoutExpired as exc:
        raw_output = (exc.stdout or b"").decode("utf-8", errors="replace") + (
            exc.stderr or b""
        ).decode("utf-8", errors="replace")
        exit_code = 124
    sanitized = raw_output.replace(dsn, "[REDACTED_DSN]").replace(password, "[REDACTED]")
    sanitized = re.sub(
        r"(?i)(password|token|secret|authorization)(\s*[=:]\s*)[^\s,]+",
        r"\1\2[REDACTED]",
        sanitized,
    )
    log_name = (
        f"database-test-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}.log"
    )
    log_path = _write_private_log(log_name, sanitized)
    summary = re.findall(r"(?im)^.*\b(?:passed|failed|skipped|error|tests? run)\b.*$", sanitized)
    print(f"test_exit_code={exit_code} private_log={log_path}")
    for line in summary[-12:]:
        print(f"test_summary={line.strip()[:220]}")
    return exit_code


def cmd_down(args: argparse.Namespace) -> None:
    """Stops only this project and preserves its bind-mounted database state."""
    if args.confirm_project != PROJECT_NAME:
        raise AcceptanceError(f"Stopping requires --confirm-project {PROJECT_NAME}.")
    resources = _verify_project_ownership()
    if not resources:
        print(f"project={PROJECT_NAME} state=already-absent")
        return
    _compose("down", timeout=120)
    print(f"project={PROJECT_NAME} state=stopped database_state_retained=True")


def _candidate_map(values: list[str]) -> dict[str, str]:
    """Validates repo-to-commit entries without accepting free-form credential data."""
    candidates: dict[str, str] = {}
    for value in values:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}=[0-9a-f]{40,64}", value):
            raise AcceptanceError("Candidate entries must use REPOSITORY=40-64_HEX_SHA format.")
        repository, sha = value.split("=", 1)
        candidates[repository] = sha
    return candidates


def cmd_record(args: argparse.Namespace) -> None:
    """Appends a sanitized evidence pointer; evidence contents are never printed or copied."""
    _ensure_state()
    if args.status == "NOT_RUN":
        if args.mode != "NOT_RUN" or not args.reason:
            raise AcceptanceError("NOT_RUN requires mode NOT_RUN and an explicit reason code.")
    elif args.mode == "NOT_RUN":
        raise AcceptanceError("A completed check must use REAL or SIMULATED mode.")
    elif not args.evidence:
        raise AcceptanceError("PASS and FAIL records require at least one evidence file.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", args.gate):
        raise AcceptanceError("Gate identifiers must be short lowercase slugs.")
    evidence_entries = []
    for raw_path in args.evidence:
        path = Path(os.path.abspath(Path(raw_path).expanduser()))
        try:
            relative = path.relative_to(EVIDENCE_DIR)
        except ValueError:
            raise AcceptanceError(
                "Evidence files must be stored under the private task evidence directory."
            ) from None
        if path.suffix.lower() not in SAFE_EVIDENCE_SUFFIXES:
            raise AcceptanceError("Evidence must use a recognized reviewable file type.")
        current = EVIDENCE_DIR
        file_info = None
        for index, component in enumerate(relative.parts):
            current = current / component
            try:
                info = current.lstat()
            except OSError:
                raise AcceptanceError(
                    "An evidence file or parent directory is unavailable."
                ) from None
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                raise AcceptanceError("Evidence paths must be owned and contain no symlink.")
            if index < len(relative.parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    raise AcceptanceError("Evidence parent components must be real directories.")
            else:
                file_info = info
        if file_info is None or not stat.S_ISREG(file_info.st_mode):
            raise AcceptanceError("Evidence references must point to regular files.")
        if file_info.st_size <= 0 or file_info.st_size > MAX_EVIDENCE_FILE_BYTES:
            raise AcceptanceError("Evidence files must be nonempty and at most 50 MiB.")
        evidence_entries.append(
            {
                "path": str(Path("evidence") / relative),
                "sha256": _sha256(current),
                "bytes": file_info.st_size,
            }
        )
    entry = {
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "stage": args.stage,
        "gate": args.gate,
        "status": args.status,
        "mode": args.mode,
        "reason": args.reason,
        "candidate_shas": _candidate_map(args.candidate),
        "evidence": evidence_entries,
    }
    line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    log_path = EVIDENCE_DIR / "evidence.jsonl"
    fd = _open_private_regular_file(
        log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600, "evidence ledger"
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    print(
        f"evidence_record={log_path} stage={args.stage} gate={args.gate} status={args.status} mode={args.mode}"
    )


def build_parser() -> argparse.ArgumentParser:
    """Builds the constrained command surface for local acceptance setup and evidence."""
    parser = argparse.ArgumentParser(
        description="Prepare the isolated Aether migration WSL acceptance environment."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser(
        "doctor", help="Report WSL, local Docker, reserved ports, and local model readiness."
    )
    doctor.set_defaults(handler=cmd_doctor)
    toolchain = subparsers.add_parser(
        "toolchain", help="Install verified Node 24.13.0 and pnpm 11.7.0 privately."
    )
    toolchain.set_defaults(handler=cmd_toolchain)
    browser_deps = subparsers.add_parser(
        "browser-deps", help="Extract verified Ubuntu browser libraries under task state only."
    )
    browser_deps.add_argument(
        "--browser", help="Playwright Chromium binary; if absent, use the installed browser cache."
    )
    browser_deps.add_argument(
        "--playwright-package",
        help="Optional installed Playwright package directory to run a browser launch smoke check.",
    )
    browser_deps.set_defaults(handler=cmd_browser_deps)
    up = subparsers.add_parser("up", help="Start only the isolated, private PostgreSQL dependency.")
    up.set_defaults(handler=cmd_up)
    status = subparsers.add_parser("status", help="Show this task's PostgreSQL container state.")
    status.set_defaults(handler=cmd_status)
    database_test = subparsers.add_parser(
        "run-with-database", help="Inject the private PostgreSQL DSN into one local test process."
    )
    database_test.add_argument("--cwd", required=True, help="Test process working directory.")
    database_test.add_argument(
        "--timeout", type=int, default=900, help="Maximum test duration in seconds."
    )
    database_test.add_argument(
        "command", nargs=argparse.REMAINDER, help="Test command following --."
    )
    database_test.set_defaults(handler=cmd_run_with_database)
    models = subparsers.add_parser(
        "models", help="Prepare and run pinned local CPU chat and embedding models."
    )
    model_actions = models.add_subparsers(dest="model_action", required=True)
    model_prepare = model_actions.add_parser(
        "prepare", help="Download official model files and verify pinned SHA-256 hashes."
    )
    model_prepare.set_defaults(handler=cmd_models_prepare)
    model_up = model_actions.add_parser(
        "up", help="Start private chat/embedding endpoints and run real inference smoke checks."
    )
    model_up.set_defaults(handler=cmd_models_up)
    model_status = model_actions.add_parser("status", help="Show task-local model endpoint status.")
    model_status.set_defaults(handler=cmd_models_status)
    speech = subparsers.add_parser(
        "speech", help="Prepare and exercise the local CPU SenseVoice Plugin runtime."
    )
    speech_actions = speech.add_subparsers(dest="speech_action", required=True)
    speech_prepare = speech_actions.add_parser(
        "prepare", help="Pin SenseVoice assets, wheels, and a private FFmpeg runtime."
    )
    speech_prepare.set_defaults(handler=cmd_speech_prepare)
    speech_smoke = speech_actions.add_parser(
        "smoke", help="Invoke a candidate speech Plugin process over loopback gRPC."
    )
    speech_smoke.add_argument(
        "--plugin-source", required=True, help="Plugins speech-sensevoice provider directory."
    )
    speech_smoke.add_argument(
        "--contracts-sdk", required=True, help="Contracts SDK Python directory."
    )
    speech_smoke.add_argument("--expected-plugin-sha", help="Require this exact Plugins commit.")
    speech_smoke.add_argument(
        "--expected-contracts-sha", help="Require this exact Contracts commit."
    )
    speech_smoke.set_defaults(handler=cmd_speech_smoke)
    identity = subparsers.add_parser(
        "identity", help="Prepare a local-only Navigator and Exchange bearer identity profile."
    )
    identity_actions = identity.add_subparsers(dest="identity_action", required=True)
    identity_prepare = identity_actions.add_parser(
        "prepare",
        help="Create owner-only local service tokens and a scoped Navigator principal map.",
    )
    identity_prepare.set_defaults(handler=cmd_identity_prepare)
    down = subparsers.add_parser(
        "down", help="Stop this task's service while retaining its database files."
    )
    down.add_argument(
        "--confirm-project",
        default="",
        help="Must exactly match the dedicated Compose project name.",
    )
    down.set_defaults(handler=cmd_down)
    record = subparsers.add_parser(
        "record", help="Append sanitized local acceptance evidence metadata."
    )
    record.add_argument("--stage", required=True, choices=[f"P{i}" for i in range(11)])
    record.add_argument(
        "--gate", required=True, help="Short gate identifier; no free-form notes or secrets."
    )
    record.add_argument("--status", required=True, choices=("PASS", "FAIL", "NOT_RUN"))
    record.add_argument("--mode", required=True, choices=("REAL", "SIMULATED", "NOT_RUN"))
    record.add_argument(
        "--reason",
        choices=(
            "DEFERRED_BY_USER",
            "CREDENTIALS_UNAVAILABLE",
            "IMPLEMENTATION_PENDING",
            "ENVIRONMENT_BLOCKED",
            "ACCEPTANCE_PENDING",
        ),
    )
    record.add_argument("--candidate", action="append", default=[], metavar="REPOSITORY=SHA")
    record.add_argument(
        "--evidence", action="append", default=[], metavar="FILE_UNDER_PRIVATE_EVIDENCE_DIR"
    )
    record.set_defaults(handler=cmd_record)
    return parser


def main() -> int:
    """Runs one safe acceptance command and emits concise status only."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except AcceptanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
