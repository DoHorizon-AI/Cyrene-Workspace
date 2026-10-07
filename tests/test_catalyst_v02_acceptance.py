"""Unit tests for fixed-input and bounded-provider acceptance safeguards.

中文：验证 v0.2 验收器的固定输入、真实 Provider 身份和一次调用保护。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = WORKSPACE_ROOT / "scripts" / "verify-catalyst-v02.py"
SPEC = importlib.util.spec_from_file_location("verify_catalyst_v02_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verifier
SPEC.loader.exec_module(verifier)


def test_fixed_manifest_verifies_native_synthetic_corpus() -> None:
    manifest = verifier._fixture_manifest()

    assert manifest["synthetic"] is True
    assert manifest["personalOrCustomerData"] is False
    assert len(manifest["files"]) == 14


def test_batch_form_uses_repeated_literal_files_field() -> None:
    manifest = verifier._fixture_manifest()
    boundary = "cyrene-v02-unit-boundary"

    body, content_type = verifier._multipart_body(
        verifier.FIXTURE_ROOT,
        manifest["files"],
        boundary,
    )

    assert content_type == f"multipart/form-data; boundary={boundary}"
    assert body.count(b'name="files[]"; filename=') == 14
    assert b"application/vnd.openxmlformats-officedocument.wordprocessingml.document" in body


def test_real_provider_identity_requires_pinned_runtime_and_one_call_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding_id = "catalyst-v02-local-qwen"
    model = "qwen2.5-1.5b-instruct-local"
    revision = verifier.EXPECTED_QWEN_REVISION
    runtime_path = tmp_path / "connector.json"
    readiness_path = tmp_path / "provider.json"
    runtime_path.write_text(
        json.dumps(
            {
                "bindingId": binding_id,
                "model": model,
                "realProvider": True,
                "completionCalls": 0,
                "ready": {
                    "event": "direct_plugin_ready",
                    "connection_ref": "grpc://127.0.0.1:36875",
                    "capability": "model.provider.v1",
                    "interface_versions": ["1"],
                },
            }
        ),
        encoding="utf-8",
    )
    readiness_path.write_text(
        json.dumps(
            {
                "revision": revision,
                "model": model,
                "source_model": "Qwen/Qwen2.5-1.5B-Instruct",
                "status": "ready",
                "bind": "127.0.0.1",
                "generation_requests_made_before_ready": 0,
                "budget": {"max_calls": 1, "max_output_tokens": 512},
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        provider_runtime=runtime_path,
        provider_readiness=readiness_path,
        provider_health_url="http://127.0.0.1:8767/healthz",
        expected_binding_id=binding_id,
        expected_model=model,
        expected_model_revision=revision,
    )
    monkeypatch.setattr(
        verifier,
        "_provider_health",
        lambda _url: {
            "status": "ready",
            "model": model,
            "source_model": "Qwen/Qwen2.5-1.5B-Instruct",
            "revision": revision,
            "endpoint": "http://127.0.0.1:8767",
            "budget": {"max_calls": 1, "calls_started": 0, "max_output_tokens": 512},
        },
    )

    identity = verifier._provider_identity(
        args,
        {
            "model_endpoint": "grpc://127.0.0.1:36875",
            "model": model,
            "max_tokens_per_call": 512,
            "timeout_seconds": 180,
        },
    )

    assert identity["realProvider"] is True
    assert identity["providerMode"] == "local-real-model-api-connector"
    assert identity["revision"] == revision
    with pytest.raises(verifier.AcceptanceFailure, match="provider identity/config mismatch"):
        verifier._provider_identity(
            SimpleNamespace(**{**vars(args), "expected_model": "fixture-scripted-provider"}),
            {
                "model_endpoint": "grpc://127.0.0.1:36875",
                "model": "fixture-scripted-provider",
                "max_tokens_per_call": 512,
                "timeout_seconds": 180,
            },
        )


def test_provider_health_rejects_non_loopback_or_non_http_urls() -> None:
    with pytest.raises(verifier.AcceptanceFailure, match="loopback HTTP /healthz"):
        verifier._provider_health("https://provider.example/healthz")


def test_skipping_restart_is_marked_partial() -> None:
    assert verifier._acceptance_status(restart_verified=True) == "PASS"
    assert verifier._acceptance_status(restart_verified=False) == "PARTIAL"


def test_provider_ledger_requires_one_completed_bounded_call() -> None:
    evidence = verifier._assert_real_usage(
        [],
        [
            {
                "status": "completed",
                "request_id": "chatcmpl-local-123",
                "model": "qwen2.5-1.5b-instruct-local",
                "revision": verifier.EXPECTED_QWEN_REVISION,
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                "usage_source": "tokenizer_ids",
            }
        ],
        model="qwen2.5-1.5b-instruct-local",
        revision=verifier.EXPECTED_QWEN_REVISION,
    )

    assert evidence["usage"]["completion_tokens"] == 50
    with pytest.raises(verifier.AcceptanceFailure, match="does not prove one completed"):
        verifier._assert_real_usage(
            [],
            [
                {
                    "status": "failed",
                    "request_id": "chatcmpl-local-123",
                    "model": "qwen2.5-1.5b-instruct-local",
                    "revision": verifier.EXPECTED_QWEN_REVISION,
                    "usage": {},
                    "usage_source": "tokenizer_ids",
                }
            ],
            model="qwen2.5-1.5b-instruct-local",
            revision=verifier.EXPECTED_QWEN_REVISION,
        )


def test_generation_call_marker_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "generation-call-issued.json"
    verifier._call_marker(path, {"maxCalls": 1})

    assert json.loads(path.read_text(encoding="utf-8"))["maxCalls"] == 1
    with pytest.raises(FileExistsError):
        verifier._call_marker(path, {"maxCalls": 1})


def test_sft_archive_rejects_operator_metadata() -> None:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "train.jsonl",
            json.dumps({"conversations": [{"from": "human", "value": "Question?"}]}) + "\n",
        )
        archive.writestr("validation.jsonl", "")
        archive.writestr("test.jsonl", "")
        archive.writestr("provenance.jsonl", verifier.MANAGEMENT_SENTINEL + "\n")
        archive.writestr("manifest.json", "{}\n")

    with pytest.raises(verifier.AcceptanceFailure, match="operator-only metadata leaked"):
        verifier._assert_sft_bundle(payload.getvalue())


def test_sft_bundle_counts_manual_and_generated_prose_families() -> None:
    expected_families = {f"family-{index:02}" for index in range(1, 13)}
    generated_source = "source-revision-prose"
    rows = []
    receipts = []
    for family in sorted(expected_families):
        for sample_index in range(2):
            rows.append(
                json.dumps(
                    {
                        "conversations": [
                            {"from": "human", "value": f"Question {family}-{sample_index}"},
                            {"from": "gpt", "value": f"Answer {family}-{sample_index}"},
                        ]
                    }
                )
            )
            receipts.append(
                json.dumps(
                    {
                        "source_family_id": family,
                        "origin": "EXTRACTED",
                        "citations": [],
                    }
                )
            )
    rows.append(
        json.dumps(
            {
                "conversations": [
                    {"from": "human", "value": "Generated prose question"},
                    {"from": "gpt", "value": "Generated prose answer"},
                ]
            }
        )
    )
    receipts.append(
        json.dumps(
            {
                "source_family_id": generated_source,
                "origin": "HUMAN_EDITED",
                "generation": {"binding_id": "local-qwen"},
                "citations": [{"source_revision_id": generated_source}],
            }
        )
    )
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("train.jsonl", "\n".join(rows) + "\n")
        archive.writestr("validation.jsonl", "")
        archive.writestr("test.jsonl", "")
        archive.writestr("provenance.jsonl", "\n".join(receipts) + "\n")
        archive.writestr(
            "manifest.json",
            json.dumps(
                {"split_stats": {"source_families": {"train": 13, "validation": 0, "test": 0}}}
            ),
        )

    result = verifier._assert_sft_bundle(
        payload.getvalue(),
        expected_manual_families=expected_families,
        expected_generated_source_id=generated_source,
    )

    assert sum(result[split] for split in ("train", "validation", "test")) == 25
    assert result["familyCount"] == 13


def test_knowledge_bundle_covers_successful_sources_and_office_evidence() -> None:
    sources = {
        "handoff.pptx": {"id": "pptx-source"},
        "handoff.xlsx": {"id": "xlsx-source"},
        "malformed.pdf": {"id": "failed-source"},
    }
    reports = {
        "handoff.pptx": {"status": "WARNING"},
        "handoff.xlsx": {"status": "SUCCEEDED"},
        "malformed.pdf": {"status": "FAILED"},
    }
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "chunks.jsonl",
            "\n".join(
                (
                    json.dumps(
                        {
                            "sourceRevisionId": "pptx-source",
                            "text": "Notes: Synthetic presenter note: verify ORCHID-42.",
                        }
                    ),
                    json.dumps(
                        {
                            "sourceRevisionId": "xlsx-source",
                            "text": "F3: formula =SUM(C3:C4); cached value unavailable",
                        }
                    ),
                )
            ),
        )
        archive.writestr(
            "manifest.json",
            json.dumps({"sourceRevisionIds": ["pptx-source", "xlsx-source"]}),
        )
        archive.writestr(
            "sources.jsonl",
            "\n".join(
                json.dumps({"sourceRevisionId": source_id})
                for source_id in ("pptx-source", "xlsx-source")
            ),
        )
        archive.writestr("hierarchy.json", "{}")
        archive.writestr("checksums.json", "{}")

    result = verifier._assert_knowledge_bundle(payload.getvalue(), sources, reports)

    assert result["coveredSuccessfulSources"] == ["handoff.pptx", "handoff.xlsx"]
    assert result["speakerNotesReadable"] is True
    assert result["xlsxFormulaEvidenceReadable"] is True
