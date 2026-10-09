#!/usr/bin/env python3
"""Run one release command with bounded retries for GitHub trust-metadata 503s.

The wrapper retries only the exact temporary attestation-service response. Failed
attempt output is buffered in temporary files so partial JSON never reaches stdout.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO

MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (2.0, 5.0)
TRUST_METADATA_503 = re.compile(
    rb"HTTP 503: trust-metadata-api service unavailable "
    rb"\(https://api\.github\.com/repos/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}/"
    rb"attestations/sha256:[0-9a-f]{64}"
    rb"(?:\?per_page=30(?:&predicate_type=https%3A%2F%2Fslsa\.dev%2Fprovenance%2Fv1)?)?\)"
)
NON_RETRYABLE_FAILURE = re.compile(
    rb"\bHTTP [45][0-9]{2}\b|\b(?:signature|signer|digest|integrity|permission|"
    rb"permissions|unauthorized|forbidden|access denied|denied|not permitted|invalid|mismatch)\b|"
    rb"\bsource (?:ref|digest|commit)\b",
    re.IGNORECASE,
)
ATTESTATION_BUNDLE_NAME = re.compile(r"sha256:[0-9a-f]{64}\.jsonl\Z")


def _has_retryable_failure(*streams: BinaryIO) -> bool:
    """Return true only for the exact GitHub trust-metadata 503 diagnostic."""

    has_retryable_diagnostic = False
    has_other_failure = False
    for stream in streams:
        stream.seek(0)
        overlap = b""
        while chunk := stream.read(64 * 1024):
            window = overlap + chunk
            has_retryable_diagnostic |= TRUST_METADATA_503.search(window) is not None
            without_retryable_diagnostic = TRUST_METADATA_503.sub(b"", window)
            has_other_failure |= (
                NON_RETRYABLE_FAILURE.search(without_retryable_diagnostic) is not None
            )
            overlap = window[-512:]
    return has_retryable_diagnostic and not has_other_failure


def _copy_to(stream: BinaryIO, destination: BinaryIO) -> None:
    """Stream a completed attempt to its destination without loading it in memory."""

    stream.seek(0)
    shutil.copyfileobj(stream, destination, length=64 * 1024)
    destination.flush()


def _discard_partial_attestation_bundle(name: str, cwd: str | os.PathLike[str] | None) -> None:
    """Remove only a generated GH attestation bundle left by a failed download."""

    if ATTESTATION_BUNDLE_NAME.fullmatch(name) is None or Path(name).name != name:
        raise ValueError("discarded attestation bundle name is not canonical")
    directory = Path.cwd() if cwd is None else Path(cwd)
    candidate = directory / name
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OSError("failed attestation download left an unsafe bundle path")
    candidate.unlink()


def run_trust_metadata_api_command(
    command: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdout: BinaryIO | None = None,
    stderr: BinaryIO | None = None,
    discard_attestation_bundle: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Run one command and retry only exact trust-metadata API HTTP 503 failures.

    Failed stdout is redirected to stderr, never the caller's JSON/report stream.
    Retries retain the original argv, environment and working directory.
    """

    if not command or any(not isinstance(argument, str) or not argument for argument in command):
        raise ValueError("command must contain non-empty string arguments")
    output = stdout if stdout is not None else sys.stdout.buffer
    errors = stderr if stderr is not None else sys.stderr.buffer

    for attempt in range(1, MAX_ATTEMPTS + 1):
        with (
            tempfile.TemporaryFile(mode="w+b") as attempt_stdout,
            tempfile.TemporaryFile(mode="w+b") as attempt_stderr,
        ):
            result = runner(
                list(command),
                cwd=cwd,
                env=env,
                stdout=attempt_stdout,
                stderr=attempt_stderr,
                check=False,
            )
            if result.returncode == 0:
                _copy_to(attempt_stdout, output)
                _copy_to(attempt_stderr, errors)
                return 0

            retryable = _has_retryable_failure(attempt_stdout, attempt_stderr)
            if discard_attestation_bundle is not None:
                _discard_partial_attestation_bundle(discard_attestation_bundle, cwd)

            if not retryable or attempt == MAX_ATTEMPTS:
                _copy_to(attempt_stdout, errors)
                _copy_to(attempt_stderr, errors)
                return result.returncode

        sleeper(RETRY_DELAYS_SECONDS[attempt - 1])

    raise AssertionError("retry loop exited without returning")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the wrapper CLI and execute the unchanged command argv."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--discard-attestation-bundle",
        help="remove this canonical sha256:<digest>.jsonl output after a failed download",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = list(arguments.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    try:
        return run_trust_metadata_api_command(
            command,
            discard_attestation_bundle=arguments.discard_attestation_bundle,
        )
    except OSError as error:
        print(f"could not run wrapped release command: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
