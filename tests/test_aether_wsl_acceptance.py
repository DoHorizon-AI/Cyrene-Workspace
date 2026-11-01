"""Security checks for the isolated Aether WSL acceptance helper.

中文:验证 Aether WSL 验收脚本的隔离路径和证据记录边界。
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts/aether_wsl_acceptance.py"
SPEC = importlib.util.spec_from_file_location("aether_wsl_acceptance", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
ACCEPTANCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCEPTANCE)


def test_secure_directory_rejects_state_root_symlink_without_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task-state link is rejected before its outside target can be chmodded."""
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    state = tmp_path / "state"
    state.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(ACCEPTANCE, "STATE_DIR", state)
    monkeypatch.setattr(ACCEPTANCE, "_validate_private_state_parent", lambda: None)

    with pytest.raises(ACCEPTANCE.AcceptanceError, match="symlink"):
        ACCEPTANCE._secure_directory(state)

    assert os.stat(outside).st_mode & 0o777 == 0o755


def test_record_refuses_completed_gate_without_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller cannot manufacture a passing result without a file receipt."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    monkeypatch.setattr(ACCEPTANCE, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(ACCEPTANCE, "_ensure_state", lambda: None)
    args = SimpleNamespace(
        status="PASS",
        mode="REAL",
        reason=None,
        evidence=[],
        gate="asr-provider",
        stage="P8",
        candidate=[],
    )

    with pytest.raises(ACCEPTANCE.AcceptanceError, match="require at least one evidence"):
        ACCEPTANCE.cmd_record(args)

    assert not (evidence_dir / "evidence.jsonl").exists()


def test_record_rejects_symlink_evidence_and_freeform_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence references must be owned regular files with constrained gate labels."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    target = evidence_dir / "receipt.json"
    target.write_text("{}\n", encoding="utf-8")
    link = evidence_dir / "linked.json"
    link.symlink_to(target)
    monkeypatch.setattr(ACCEPTANCE, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(ACCEPTANCE, "_ensure_state", lambda: None)
    args = SimpleNamespace(
        status="PASS",
        mode="REAL",
        reason=None,
        evidence=[str(link)],
        gate="asr-provider",
        stage="P8",
        candidate=[],
    )

    with pytest.raises(ACCEPTANCE.AcceptanceError, match="symlink"):
        ACCEPTANCE.cmd_record(args)

    args.evidence = [str(target)]
    args.gate = "PASS because it worked"
    with pytest.raises(ACCEPTANCE.AcceptanceError, match="lowercase slugs"):
        ACCEPTANCE.cmd_record(args)

    assert not (evidence_dir / "evidence.jsonl").exists()


def test_local_identity_prepare_is_private_scoped_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Local bearer setup writes owner-only files and never prints token values."""
    state = tmp_path / "state"
    evidence = state / "evidence"
    monkeypatch.setattr(ACCEPTANCE, "STATE_DIR", state)
    monkeypatch.setattr(ACCEPTANCE, "TOOLCHAIN_DIR", state / "toolchain")
    monkeypatch.setattr(ACCEPTANCE, "EVIDENCE_DIR", evidence)
    monkeypatch.setattr(ACCEPTANCE, "PRIVATE_STATE_PARENT", tmp_path)
    monkeypatch.setattr(ACCEPTANCE, "_validate_private_state_parent", lambda: None)
    monkeypatch.setattr(ACCEPTANCE, "_require_wsl2_x64", lambda: None)
    ACCEPTANCE._ensure_state()
    ACCEPTANCE._write_private_file(
        evidence / "local-model-endpoints.json",
        json.dumps(
            {
                "status": "PASS",
                "classification": "REAL",
                "wsl_host_chat_endpoint": "http://10.247.253.4:8080/v1",
                "wsl_host_embedding_endpoint": "http://10.247.253.3:8081/v1",
            }
        ),
    )
    pinned_e5 = next(
        item
        for item in ACCEPTANCE.MODEL_ARTIFACTS
        if item["model_id"] == "intfloat/multilingual-e5-small"
        and item["relative_path"] == "onnx/model_qint8_avx512_vnni.onnx"
    )
    ACCEPTANCE._write_private_file(
        state / "models/manifest.json",
        json.dumps(
            {
                "artifacts": [
                    {
                        "model_id": pinned_e5["model_id"],
                        "relative_path": pinned_e5["relative_path"],
                        "revision": pinned_e5["revision"],
                        "sha256": pinned_e5["sha256"],
                    }
                ]
            }
        ),
    )

    ACCEPTANCE.cmd_identity_prepare(SimpleNamespace())
    first_output = capsys.readouterr().out
    env_path = state / "local-product/services.env"
    principal_path = state / "local-product/navigator-principals.json"
    profile_path = state / "local-product/embedding-profile.json"
    values = ACCEPTANCE._read_private_env(env_path, "test local identity")
    principals = ACCEPTANCE._read_private_json(principal_path, "test principal config")
    secret_values = [values[name] for name in values if name.endswith("TOKEN")]
    assert all(secret and secret not in first_output for secret in secret_values)
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert principal_path.stat().st_mode & 0o777 == 0o600
    assert principals["principals"][0]["actor_id"] == "aether-local-acceptance"
    assert principals["principals"][0]["workspace_ids"] == ["local"]
    assert principals["principals"][1]["actor_private_scope"] is True
    assert principals["principals"][1].get("can_write_harness", False) is False
    profile = ACCEPTANCE._read_private_json(profile_path, "test embedding profile")
    assert profile["dimensions"] == 384
    assert profile["document_prefix"] == "passage: "
    assert profile["query_prefix"] == "query: "
    assert profile["model_sha256"] == pinned_e5["sha256"]
    expected_chat = "http://10.247.253.4:8080/v1"
    expected_embeddings = "http://10.247.253.3:8081/v1"

    extra_environment = dict(values)
    extra_environment["CYRENE_PROXY_URL"] = "https://unexpected.invalid"
    with pytest.raises(ACCEPTANCE.AcceptanceError, match="extra or missing keys"):
        ACCEPTANCE._validate_local_identity(
            extra_environment, principals, expected_chat, expected_embeddings
        )

    duplicate_tokens = dict(values)
    duplicate_tokens["CYRENE_EXCHANGE_BOOTSTRAP_BEARER_TOKEN"] = values[
        "CYRENE_EXCHANGE_BEARER_TOKEN"
    ]
    with pytest.raises(ACCEPTANCE.AcceptanceError, match="distinct bearer"):
        ACCEPTANCE._validate_local_identity(
            duplicate_tokens, principals, expected_chat, expected_embeddings
        )

    expanded_principal = json.loads(json.dumps(principals))
    expanded_principal["principals"][1]["can_write_harness"] = True
    with pytest.raises(ACCEPTANCE.AcceptanceError, match="unsupported grants"):
        ACCEPTANCE._validate_local_identity(
            values, expanded_principal, expected_chat, expected_embeddings
        )

    ACCEPTANCE.cmd_identity_prepare(SimpleNamespace())
    capsys.readouterr()
    assert ACCEPTANCE._read_private_env(env_path, "test local identity") == values


def test_private_env_reader_rejects_symlink(tmp_path: Path) -> None:
    """The local service environment reader does not follow a symlink target."""
    target = tmp_path / "outside.env"
    target.write_text("CYRENE_TOKEN=private\n", encoding="utf-8")
    os.chmod(target, 0o600)
    link = tmp_path / "services.env"
    link.symlink_to(target)

    with pytest.raises(ACCEPTANCE.AcceptanceError, match="opened safely"):
        ACCEPTANCE._read_private_env(link, "test environment")


def test_private_env_reader_rejects_oversized_file(tmp_path: Path) -> None:
    """The env reader checks descriptor size before parsing bounded content."""
    path = tmp_path / "oversized.env"
    path.write_bytes(b"\n" * (64 * 1024 + 1))
    os.chmod(path, 0o600)

    with pytest.raises(ACCEPTANCE.AcceptanceError, match="exceeds its size bound"):
        ACCEPTANCE._read_private_env(path, "test environment")
