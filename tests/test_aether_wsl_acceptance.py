"""Security checks for the isolated Aether WSL acceptance helper.

中文:验证 Aether WSL 验收脚本的隔离路径和证据记录边界。
"""

from __future__ import annotations

import importlib.util
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
