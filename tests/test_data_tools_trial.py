"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_data_tools_trial.py                                         │
│  Module: tests.test_data_tools_trial                                 │
│  Role: Validate the fixed data-tools pilot corpus and live gate.     │
│                                                                      │
│  模块职责：校验固定试用语料，并提供可选的真实服务验收门禁。               │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "data-tools-trial"
PRIVATE_MARKER = "TRIAL_INTERNAL_REVIEW_NOTE_DO_NOT_TRAIN_7f4a"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Read a complete JSONL fixture and require each row to be an object.

    完整读取 JSONL 夹具，并要求每行均为 JSON 对象。
    """
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        assert isinstance(value, dict)
        rows.append(value)
    return rows


def test_fixed_corpus_manifest_and_duplicate_source_policy() -> None:
    """Keep byte-identical uploads logically separate with opposite policies.

    验证相同字节可以对应独立来源，并配置互斥用途权限。
    """
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    for name, expected_digest in manifest["files"].items():
        assert hashlib.sha256((FIXTURE_ROOT / name).read_bytes()).hexdigest() == expected_digest
    scenario = manifest["duplicateSourceScenario"]
    assert scenario["path"] == "trial-handbook.pdf"
    raw = (FIXTURE_ROOT / scenario["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == scenario["sha256"]
    uploads = scenario["uploads"]
    assert [item["alias"] for item in uploads] == [
        "knowledge-only-copy",
        "training-only-copy",
    ]
    knowledge_policy, training_policy = (item["policy"] for item in uploads)
    assert knowledge_policy["allowKnowledge"] is True
    assert knowledge_policy["allowTraining"] is False
    assert training_policy["allowKnowledge"] is False
    assert training_policy["allowTraining"] is True
    assert knowledge_policy["allowedPrincipalRefs"] != training_policy["allowedPrincipalRefs"]
    assert knowledge_policy["allowedUsePurposes"] != training_policy["allowedUsePurposes"]


def test_pdf_docx_and_invalid_inputs_are_fixed_and_parseable_as_containers() -> None:
    """Validate the PDF signature, Unicode map and DOCX OpenXML table.

    验证 PDF 结构、Unicode 映射，以及 DOCX OpenXML 中的真实表格。
    """
    pdf = (FIXTURE_ROOT / "trial-handbook.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-1.7")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"/ToUnicode 8 0 R" in pdf
    assert "来源键".encode("utf-16-be").hex().upper().encode("ascii") in pdf

    with ZipFile(FIXTURE_ROOT / "trial-handbook.docx") as archive:
        assert archive.testzip() is None
        document = ET.fromstring(archive.read("word/document.xml"))
    text = "".join(node.text or "" for node in document.iter(f"{W_NS}t"))
    assert "中文段落" in text
    assert "🌱🧭" in text
    assert "知识输出" in text and "训练输出" in text
    assert len(list(document.iter(f"{W_NS}tbl"))) == 1
    assert PRIVATE_MARKER in text

    malformed_pdf = (FIXTURE_ROOT / "malformed.pdf").read_bytes()
    assert malformed_pdf.startswith(b"%PDF-")
    assert not malformed_pdf.rstrip().endswith(b"%%EOF")
    with pytest.raises(json.JSONDecodeError):
        _read_jsonl(FIXTURE_ROOT / "malformed.jsonl")


def test_training_and_echo_corpora_match_expected_acceptance_counts() -> None:
    """Keep grouped SFT inputs and exact-match rows stable across runs.

    固定训练分组及 Echo 精确匹配样例，确保验收结果可复现。
    """
    training = _read_jsonl(FIXTURE_ROOT / "trial-training.jsonl")
    assert len(training) == 20
    assert len({row["sampleId"] for row in training}) == 20
    groups = {row["sourceFamily"] for row in training}
    assert len(groups) == 10
    expected_split_by_family = json.loads(
        (FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
    )["training"]["expectedSplitByFamily"]
    assert set(expected_split_by_family) == groups
    assert set(expected_split_by_family.values()) == {"train", "validation", "test"}
    assert all(sum(row["sourceFamily"] == group for row in training) == 2 for group in groups)
    assert all(row["conversationId"] == row["sourceFamily"] for row in training)
    assert all(row["_reviewNote"] == PRIVATE_MARKER for row in training)

    echo_rows = _read_jsonl(FIXTURE_ROOT / "echo-reference-actual.jsonl")
    assert len(echo_rows) == 4
    assert len({row["sampleId"] for row in echo_rows}) == 4
    evaluated = [
        row
        for row in echo_rows
        if isinstance(row.get("reference"), str) and isinstance(row.get("actual"), str)
    ]
    skipped = [row for row in echo_rows if row not in evaluated]
    matched = sum(row["reference"] == row["actual"] for row in evaluated)
    assert (len(evaluated), len(skipped), matched) == (2, 2, 1)
    duplicate_rows = _read_jsonl(FIXTURE_ROOT / "echo-duplicate-sample-id.jsonl")
    assert [row["sampleId"] for row in duplicate_rows] == [
        "duplicate-sample",
        "duplicate-sample",
    ]


def _sft_package(provenance: list[dict[str, str]]) -> bytes:
    """Build a tiny SFT package in the worker's established flat ZIP layout.

    按插件已冻结的 flat ZIP 布局构造小型 SFT 包。
    """
    members = {
        "train.jsonl": b'{"instruction":"q1","output":"a1"}\n{"instruction":"q2","output":"a2"}\n',
        "validation.jsonl": b'{"instruction":"q3","output":"a3"}\n',
        "test.jsonl": b'{"instruction":"q4","output":"a4"}\n',
        "provenance.jsonl": b"".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            for row in provenance
        ),
    }
    manifest = {
        "schema_version": "cyrene.sft.bundle.v1",
        "split_algorithm": "sha256-source-family-v1",
        "split_stats": {
            "algorithm": "sha256-source-family-v1",
            "ratios": {"train": 0.8, "validation": 0.1, "test": 0.1},
            "samples": {"train": 2, "validation": 1, "test": 1},
            "source_families": {"train": 1, "validation": 1, "test": 1},
        },
        "files": {
            name: {"digest": "sha256:" + hashlib.sha256(payload).hexdigest(), "row_count": len(payload.splitlines())}
            for name, payload in members.items()
        },
    }
    members["manifest.json"] = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return output.getvalue()


def _trial_verifier_module():
    """Load the standalone acceptance verifier for direct unit checks.

    加载独立验收脚本，以便对其包级断言做定向测试。
    """
    script = Path(__file__).parents[1] / "scripts" / "verify-data-tools-trial.py"
    spec = importlib.util.spec_from_file_location("verify_data_tools_trial", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sft_verifier_uses_provenance_sidecar_to_check_family_isolation() -> None:
    """Validate group-disjoint splits without requiring private IDs in answer rows.

    使用 provenance sidecar 验证来源组互斥，学习行不承载管理 ID。
    """
    provenance = [
        {"sample_id": "a1", "source_family_id": "family-a", "split": "train", "policy": {"allow_training": True}},
        {"sample_id": "a2", "source_family_id": "family-a", "split": "train", "policy": {"allow_training": True}},
        {"sample_id": "b1", "source_family_id": "family-b", "split": "validation", "policy": {"allow_training": True}},
        {"sample_id": "c1", "source_family_id": "family-c", "split": "test", "policy": {"allow_training": True}},
    ]
    verifier = _trial_verifier_module()
    counts = verifier._check_sft_package(
        _sft_package(provenance), {"family-a", "family-b", "family-c"}
    )
    assert counts == {"train": 2, "validation": 1, "test": 1}

    contaminated = [
        *provenance[:2],
        {"sample_id": "b1", "source_family_id": "family-a", "split": "validation", "policy": {"allow_training": True}},
        provenance[3],
    ]
    with pytest.raises(verifier.TrialFailure, match="leaked between"):
        verifier._check_sft_package(_sft_package(contaminated), set())


def test_data_tools_trial_verifier_fixture_mode() -> None:
    """Expose the same local corpus checks through the operator CLI.

    通过运维命令行公开相同的本地夹具检查。
    """
    script = Path(__file__).parents[1] / "scripts" / "verify-data-tools-trial.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--fixtures-only"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    assert "FIXTURES PASS" in completed.stdout


@pytest.mark.skipif(
    os.environ.get("CYRENE_DATA_TOOLS_RUN_E2E") != "1",
    reason="Set CYRENE_DATA_TOOLS_RUN_E2E=1 and trial service URLs to run live acceptance.",
)
def test_live_data_tools_trial_acceptance() -> None:
    """Run the real Product APIs only when explicitly opted in.

    仅在显式 opt-in 后调用真实 Product API。
    """
    script = Path(__file__).parents[1] / "scripts" / "verify-data-tools-trial.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "TRIAL ACCEPTANCE PASS" in completed.stdout


@pytest.mark.skipif(
    os.environ.get("CYRENE_DATA_TOOLS_RUN_LAUNCHER_SMOKE") != "1",
    reason="Set CYRENE_DATA_TOOLS_RUN_LAUNCHER_SMOKE=1 to build and start the real Linux trial stack.",
)
def test_native_linux_launcher_startup_and_shutdown() -> None:
    """Exercise the packaged Products and real Plugin readiness records.

    启动实际 Product 与 Plugin，再通过 launcher 正常停止。
    """
    script = Path(__file__).parents[1] / "scripts" / "verify-data-tools-trial.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--launcher-smoke"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=700,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "LAUNCHER SMOKE PASS" in completed.stdout
