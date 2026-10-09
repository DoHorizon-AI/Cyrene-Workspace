"""Focused tests for bounded retry of exact GitHub trust-metadata 503s."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import retry_trust_metadata_api as retry_helper


def _503(repository: str = "DoHorizon-AI/Cyrene-Platform", *, query: str = "per_page=30") -> bytes:
    return (
        "Failed to download the artifact's bundle(s): failed to fetch attestations: "
        "HTTP 503: trust-metadata-api service unavailable "
        f"(https://api.github.com/repos/{repository}/attestations/sha256:{'a' * 64}?{query})\n"
    ).encode()


def _runner_from(attempts: list[tuple[bytes, bytes, int]], calls: list[list[str]]) -> Any:
    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(command)
        stdout, stderr, returncode = attempts.pop(0)
        kwargs["stdout"].write(stdout)
        kwargs["stderr"].write(stderr)
        return SimpleNamespace(returncode=returncode)

    return run


@pytest.mark.parametrize(
    "query",
    [
        "per_page=30",
        "per_page=30&predicate_type=https%3A%2F%2Fslsa.dev%2Fprovenance%2Fv1",
    ],
)
def test_retry_after_exact_503_keeps_only_successful_json_stdout(query: str) -> None:
    command = ["python", "fetch_component.py", "--source-commit", "a" * 40]
    calls: list[list[str]] = []
    delays: list[float] = []
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    result = retry_helper.run_trust_metadata_api_command(
        command,
        env={"GH_TOKEN": "unchanged"},
        stdout=stdout,
        stderr=stderr,
        runner=_runner_from(
            [(b'{"partial":', _503(query=query), 1), (b'{"ok":true}\n', b"warning\n", 0)],
            calls,
        ),
        sleeper=delays.append,
    )

    assert result == 0
    assert calls == [command, command]
    assert delays == [2.0]
    assert stdout.getvalue() == b'{"ok":true}\n'
    assert stderr.getvalue() == b"warning\n"


def test_exact_503_stops_after_three_attempts_and_returns_failure() -> None:
    calls: list[list[str]] = []
    delays: list[float] = []
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    command = ["gh", "attestation", "download", "artifact.tar.gz"]

    result = retry_helper.run_trust_metadata_api_command(
        command,
        stdout=stdout,
        stderr=stderr,
        runner=_runner_from([(b"partial\n", _503(), 1)] * 3, calls),
        sleeper=delays.append,
    )

    assert result == 1
    assert calls == [command, command, command]
    assert delays == [2.0, 5.0]
    assert stdout.getvalue() == b""
    assert stderr.getvalue() == b"partial\n" + _503()


@pytest.mark.parametrize(
    "diagnostic",
    [
        b"HTTP 503: github.com is temporarily unavailable\n",
        b"HTTP 503: trust-metadata-api service unavailable (https://example.com/attestations)\n",
        _503("DoHorizon-AI/Cyrene-Plugins-Official") + b"HTTP 403: signature invalid\n",
    ],
)
def test_unrelated_or_mixed_failure_is_not_retried(diagnostic: bytes) -> None:
    calls: list[list[str]] = []
    delays: list[float] = []
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    command = ["gh", "attestation", "download", "artifact.tar.gz"]

    result = retry_helper.run_trust_metadata_api_command(
        command,
        stdout=stdout,
        stderr=stderr,
        runner=_runner_from([(b"not-json\n", diagnostic, 1)], calls),
        sleeper=delays.append,
    )

    assert result == 1
    assert calls == [command]
    assert delays == []
    assert stdout.getvalue() == b""
    assert stderr.getvalue() == b"not-json\n" + diagnostic


def test_cli_streams_only_successful_attempt_output(tmp_path: Path) -> None:
    helper = SCRIPTS / "retry_trust_metadata_api.py"
    counter = tmp_path / "attempts"
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(attempt))\n"
        "if attempt == 1:\n"
        "    print('{\\\"partial\\\":', flush=True)\n"
        f"    print({_503().decode().strip()!r}, file=sys.stderr, flush=True)\n"
        "    raise SystemExit(1)\n"
        "print('{\\\"ok\\\":true}')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(helper), "--", sys.executable, str(child)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert counter.read_text(encoding="utf-8") == "2"
    assert result.stdout == '{"ok":true}\n'
    assert "trust-metadata-api" not in result.stderr


def test_failed_attestation_download_removes_only_its_generated_bundle(tmp_path: Path) -> None:
    bundle_name = f"sha256:{'b' * 64}.jsonl"
    (tmp_path / bundle_name).write_text("partial", encoding="utf-8")
    calls: list[list[str]] = []
    output = io.BytesIO()
    errors = io.BytesIO()

    result = retry_helper.run_trust_metadata_api_command(
        ["gh", "attestation", "download", "artifact.tar.gz"],
        cwd=tmp_path,
        stdout=output,
        stderr=errors,
        discard_attestation_bundle=bundle_name,
        runner=_runner_from([(b"", b"HTTP 403: forbidden\n", 1)], calls),
        sleeper=lambda _delay: None,
    )

    assert result == 1
    assert not (tmp_path / bundle_name).exists()


def test_workspace_local_verifier_uses_retry_wrapper_without_changing_gh_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release_path = SCRIPTS / "native_installer_release.py"
    spec = importlib.util.spec_from_file_location(
        "native_installer_release_retry_test", release_path
    )
    assert spec is not None and spec.loader is not None
    release = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = release
    spec.loader.exec_module(release)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    subject = tmp_path / "component.tar.gz"
    bundle = tmp_path / "component.tar.gz.attestation.jsonl"

    release._run_attestation_verify(
        subject,
        bundle,
        repository="DoHorizon-AI/Cyrene-Platform",
        workflow="DoHorizon-AI/Cyrene-Platform/.github/workflows/component-release.yml",
        source_ref="refs/heads/develop",
        source_commit="c" * 40,
        gh_executable="gh",
        label="Platform component",
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:3] == [
        sys.executable,
        str(release_path.with_name("retry_trust_metadata_api.py")),
        "--",
    ]
    assert command[3:] == [
        "gh",
        "attestation",
        "verify",
        str(subject),
        "--bundle",
        str(bundle),
        "--repo",
        "DoHorizon-AI/Cyrene-Platform",
        "--signer-workflow",
        "DoHorizon-AI/Cyrene-Platform/.github/workflows/component-release.yml",
        "--source-ref",
        "refs/heads/develop",
        "--source-digest",
        "c" * 40,
        "--predicate-type",
        "https://slsa.dev/provenance/v1",
    ]
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.PIPE


def test_release_workflow_wraps_fetch_and_both_bundle_downloads() -> None:
    workflow = (SCRIPTS.parent / ".github/workflows/native-installer-release.yml").read_text(
        encoding="utf-8"
    )

    assert "python scripts/retry_trust_metadata_api.py -- \\" in workflow
    assert "python packaging/fetch_component_catalog.py \\" in workflow
    assert 'python "$PLATFORM_FETCHER" \\' in workflow
    assert workflow.count("gh attestation download") == 2
    assert workflow.count('--discard-attestation-bundle "sha256:$digest.jsonl"') == 1
    assert workflow.count('--discard-attestation-bundle "$digest.jsonl"') == 1
