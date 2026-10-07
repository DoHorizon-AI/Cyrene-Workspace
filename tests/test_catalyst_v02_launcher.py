"""Tests for the Catalyst v0.2 native launcher preset.

中文：验证 Catalyst v0.2 原生启动预设及隔离 OCR 路径。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "catalyst_v02_trial_launcher_test",
    WORKSPACE_ROOT / "packaging" / "data_tools_trial.py",
)
assert SPEC is not None and SPEC.loader is not None
trial = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trial
SPEC.loader.exec_module(trial)


def test_catalyst_only_is_opt_in_and_excludes_echo_evaluation_plugin() -> None:
    default_args = trial.build_parser().parse_args(["start"])
    v02_args = trial.build_parser().parse_args(["start", "--catalyst-only"])

    assert default_args.catalyst_only is False
    assert [spec[0] for spec in trial._plugin_specs_for(False)] == [
        "document-parsing",
        "knowledge-preparation",
        "dataset-generation",
        "dataset-preparation",
        "exact-match",
    ]
    assert [spec[0] for spec in trial._plugin_specs_for(v02_args.catalyst_only)] == [
        "document-parsing",
        "knowledge-preparation",
        "dataset-generation",
        "dataset-preparation",
    ]


def test_parser_environment_uses_only_task_local_tesseract_and_docling_assets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ocr-root"
    docling = tmp_path / "docling-artifacts"
    environment = trial._document_parser_environment(
        {"LD_LIBRARY_PATH": "/system/lib", "PATH": "/usr/bin"},
        ocr_runtime_root=root,
        languages="eng,chi_sim",
        engine="auto",
        dpi=200,
        minimum_confidence=0.8,
        docling_artifacts_path=docling,
    )

    assert environment["CYRENE_DOCUMENT_PARSING_TESSERACT_CMD"] == str(root / "usr/bin/tesseract")
    assert environment["CYRENE_DOCUMENT_PARSING_TESSDATA_PREFIX"] == str(
        root / "usr/share/tesseract-ocr/5/tessdata"
    )
    assert environment["LD_LIBRARY_PATH"].split(os.pathsep) == [
        str(root / "usr/lib/x86_64-linux-gnu"),
        "/system/lib",
    ]
    assert environment["CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH"] == str(docling)
    assert "CYRENE_DOCUMENT_PARSING_OCR_ARTIFACTS_PATH" not in environment
