#!/usr/bin/env python3
"""Verify the real Catalyst v0.2 source-to-published-artifacts workflow.

中文：通过固定合成混合语料验收真实解析、人工审核、一次模型生成、双制品发布与重启恢复。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

TASK_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = TASK_ROOT / "workspace/tests/fixtures/catalyst-v02"
DEFAULT_PROVIDER_RUNTIME = TASK_ROOT / "reports/model-api-connector-runtime.json"
DEFAULT_PROVIDER_READINESS = TASK_ROOT / "reports/local-provider-readiness.json"
DEFAULT_PROVIDER_USAGE = TASK_ROOT / "reports/local-provider-usage.jsonl"
DEFAULT_PROVIDER_HEALTH_URL = "http://127.0.0.1:8767/healthz"
DEFAULT_GENERATION_CONFIG = TASK_ROOT / "generation-config.json"
DEFAULT_STATE_DIR = Path.home() / ".local/state/cyrene/catalyst-v02-20261007"
MANAGEMENT_SENTINEL = "ORCHID42_OPERATOR_ONLY_SENTINEL_DO_NOT_TRAIN_20261007"
TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
TERMINAL_REPORT_STATES = {"SUCCEEDED", "WARNING", "FAILED", "INTERRUPTED", "CANCELLED"}
EXPECTED_QWEN_REVISION = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"


class AcceptanceFailure(RuntimeError):
    """Describe an acceptance failure without exposing credentials or source text."""


class ApiClient:
    """Call one local or remote Product endpoint without logging auth headers."""

    def __init__(self, base_url: str, token: str | None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Send one request and return status, response headers, and body."""

        headers = {"Accept": "application/json"}
        request_body = body
        if json_body is not None:
            request_body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            content_type = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path,
            data=request_body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers.items()), error.read()
        except (urllib.error.URLError, TimeoutError) as error:
            raise AcceptanceFailure(f"{method} {path} could not reach the Product API") from error

    def json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        expected: set[int] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        """Send one request and decode its JSON response."""

        status, headers, payload = self.request(
            method,
            path,
            json_body=json_body,
            body=body,
            content_type=content_type,
            timeout=timeout,
        )
        if expected is not None and status not in expected:
            raise AcceptanceFailure(
                f"{method} {path} returned HTTP {status}; expected {sorted(expected)}; "
                f"{_error_detail(payload)}"
            )
        if not payload:
            return status, headers, None
        try:
            return status, headers, json.loads(payload)
        except json.JSONDecodeError as error:
            raise AcceptanceFailure(f"{method} {path} returned invalid JSON") from error


def _error_detail(payload: bytes) -> str:
    """Return a short sanitized API error summary."""

    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "non-JSON error response"
    if isinstance(value, dict):
        code = value.get("code") or value.get("type") or "API_ERROR"
        message = value.get("detail") or value.get("title") or value.get("message") or ""
        return f"{str(code)[:100]}: {str(message)[:180]}"
    return "API returned an error"


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    """Read one regular non-symlink JSON object from the task tree."""

    if path.is_symlink() or not path.is_file():
        raise AcceptanceFailure(f"{label} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceFailure(f"{label} is invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise AcceptanceFailure(f"{label} must be a JSON object")
    return value


def _fixture_manifest(fixture_root: Path = FIXTURE_ROOT) -> dict[str, Any]:
    """Verify all fixed native-format corpus bytes against the manifest."""

    manifest = _json_object(fixture_root / "manifest.json", label="fixture manifest")
    files = manifest.get("files")
    if (
        manifest.get("synthetic") is not True
        or manifest.get("personalOrCustomerData") is not False
        or not isinstance(files, dict)
        or not files
        or len(files) > 20
    ):
        raise AcceptanceFailure("fixture manifest is not the fixed synthetic corpus")
    for name, record in files.items():
        path = fixture_root / name
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or path.is_symlink()
            or not path.is_file()
            or not isinstance(record, dict)
        ):
            raise AcceptanceFailure("fixture manifest contains an unsafe file entry")
        payload = path.read_bytes()
        if (
            hashlib.sha256(payload).hexdigest() != record.get("sha256")
            or len(payload) != record.get("sizeBytes")
            or len(payload) > 32 * 1024 * 1024
            or not isinstance(record.get("mediaType"), str)
        ):
            raise AcceptanceFailure(f"fixed fixture receipt changed: {name}")
    expected_files = {
        "handoff-native.pdf",
        "handoff-scanned.pdf",
        "handoff.docx",
        "handoff.pptx",
        "handoff.xlsx",
        "handoff-ocr-clear.png",
        "handoff-ocr-clear.jpg",
        "handoff-ocr-low-quality.jpg",
        "handoff.md",
        "handoff.txt",
        "handoff.csv",
        "source-conversations.jsonl",
        "malformed.pdf",
        "unsupported.rtf",
    }
    if set(files) != expected_files:
        raise AcceptanceFailure("fixture manifest does not contain the expected mixed formats")
    if manifest.get("managementSentinel") != MANAGEMENT_SENTINEL:
        raise AcceptanceFailure("fixture metadata sentinel changed")
    return manifest


def _fixture_source_families(fixture_root: Path) -> set[str]:
    """Return the 12 validated manual fixture families for SFT evidence."""

    path = fixture_root / "source-conversations.jsonl"
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceFailure("fixed conversation fixture is invalid JSONL") from error
    if len(rows) != 24 or any(not isinstance(row, dict) for row in rows):
        raise AcceptanceFailure("fixed conversation fixture must contain 24 records")
    families = {row.get("sourceFamily") for row in rows}
    if len(families) != 12 or any(
        not isinstance(family, str) or not family.startswith("family-") for family in families
    ):
        raise AcceptanceFailure("fixed conversation fixture must contain 12 manual families")
    return families


def _multipart_body(
    fixture_root: Path,
    files: dict[str, Any],
    boundary: str,
) -> tuple[bytes, str]:
    """Build the contract's repeated literal `files[]` multipart body."""

    chunks: list[bytes] = []
    for filename, record in files.items():
        media_type = str(record["mediaType"])
        payload = (fixture_root / filename).read_bytes()
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="files[]"; filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {media_type}\r\n\r\n".encode("ascii"),
                payload,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    body = b"".join(chunks)
    if len(body) > 129 * 1024 * 1024:
        raise AcceptanceFailure("fixed batch exceeds the Catalyst multipart request bound")
    return body, f"multipart/form-data; boundary={boundary}"


def _pick(document: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Read one contract field while accepting Python and JSON aliases."""

    for name in names:
        if name in document:
            return document[name]
    return default


def _id(document: dict[str, Any], label: str = "resource") -> str:
    """Return a resource UUID from one Product response object."""

    value = _pick(document, "id", "datasetId", "sourceRevisionId", "revisionId", "runId")
    if not isinstance(value, str) or not value:
        raise AcceptanceFailure(f"{label} response is missing its id")
    return value


def _canonical_digest(value: Any) -> str:
    """Hash the stable JSON representation of one API snapshot."""

    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _terminal_run(client: ApiClient, run_id: str, timeout: float) -> dict[str, Any]:
    """Poll one admitted run and fail on terminal errors without retrying it."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, _, run = client.json("GET", f"/api/v1/processing-runs/{run_id}", expected={200})
        if not isinstance(run, dict):
            raise AcceptanceFailure("ProcessingRun response is not an object")
        state = str(_pick(run, "state") or "").upper()
        if state in TERMINAL_RUN_STATES:
            if state != "SUCCEEDED":
                failure = run.get("failure")
                code = failure.get("code") if isinstance(failure, dict) else "unknown"
                raise AcceptanceFailure(f"ProcessingRun {run_id} ended {state} ({code})")
            return run
        time.sleep(0.5)
    raise AcceptanceFailure(f"ProcessingRun {run_id} exceeded its {timeout:g}s deadline")


def _wait_parse_reports(
    client: ApiClient,
    dataset_id: str,
    run_id: str,
    expected_count: int,
    timeout: float,
) -> list[dict[str, Any]]:
    """Wait until each source has one persisted terminal per-file report."""

    query = urllib.parse.urlencode({"processingRunId": run_id})
    path = f"/api/v1/datasets/{dataset_id}/source-parse-reports?{query}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, _, reports = client.json("GET", path, expected={200})
        if isinstance(reports, list) and len(reports) == expected_count:
            states = {str(_pick(row, "status")).upper() for row in reports if isinstance(row, dict)}
            if len(states) == 1 and states <= TERMINAL_REPORT_STATES:
                return reports
            if states and states <= TERMINAL_REPORT_STATES:
                return reports
        time.sleep(0.5)
    raise AcceptanceFailure("per-source parse reports did not become durable and terminal")


def _run(client: ApiClient, dataset_id: str, operation: str, **values: Any) -> dict[str, Any]:
    """Admit exactly one run for the supplied operation and selected revision."""

    body = {"operation": operation, **values}
    _, _, response = client.json(
        "POST",
        f"/api/v1/datasets/{dataset_id}/processing-runs",
        json_body=body,
        expected={202},
    )
    if not isinstance(response, dict):
        raise AcceptanceFailure(f"{operation} admission did not return a ProcessingRun")
    return response


def _revision(client: ApiClient, dataset_id: str, revision_id: str) -> dict[str, Any]:
    """Find one Product-owned immutable revision in the dataset list."""

    _, _, revisions = client.json(
        "GET", f"/api/v1/datasets/{dataset_id}/content-revisions", expected={200}
    )
    if not isinstance(revisions, list):
        raise AcceptanceFailure("content revision list is not an array")
    for item in revisions:
        if isinstance(item, dict) and _pick(item, "id", "revisionId") == revision_id:
            return item
    raise AcceptanceFailure(f"ContentRevision {revision_id} is not in this Dataset")


def _revision_blocks(client: ApiClient, revision_id: str) -> list[dict[str, Any]]:
    """Read a bounded first page containing every fixed corpus block."""

    _, _, page = client.json(
        "GET",
        f"/api/v1/content-revisions/{revision_id}/blocks?offset=0&limit=200",
        expected={200},
    )
    blocks = page.get("blocks") if isinstance(page, dict) else None
    if not isinstance(blocks, list) or any(not isinstance(item, dict) for item in blocks):
        raise AcceptanceFailure("ContentBlock page is invalid")
    if len(blocks) >= 200:
        raise AcceptanceFailure("fixed v0.2 corpus unexpectedly exceeds one review page")
    return blocks


def _queue(client: ApiClient, dataset_id: str) -> dict[str, Any]:
    """Read the review queue and generated-draft projections."""

    _, _, queue = client.json("GET", f"/api/v1/datasets/{dataset_id}/review-queue", expected={200})
    if (
        not isinstance(queue, dict)
        or not isinstance(queue.get("items"), list)
        or not isinstance(queue.get("generatedDrafts"), list)
    ):
        raise AcceptanceFailure("review queue must contain items and generatedDrafts arrays")
    return queue


def _assert_parser_evidence(
    reports: list[dict[str, Any]],
    sources_by_name: dict[str, dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assert native extraction, OCR provenance, structural warnings, and failures."""

    report_by_source = {
        str(_pick(report, "sourceRevisionId", "source_revision_id")): report
        for report in reports
        if isinstance(report, dict)
    }
    if len(report_by_source) != len(sources_by_name):
        raise AcceptanceFailure("parse reports do not cover every uploaded SourceRevision")
    reports_by_name: dict[str, dict[str, Any]] = {}
    for filename, source in sources_by_name.items():
        report = report_by_source.get(_id(source, "SourceRevision"))
        if report is None:
            raise AcceptanceFailure(f"parse report is missing for {filename}")
        reports_by_name[filename] = report
    status = lambda filename: str(_pick(reports_by_name[filename], "status")).upper()
    if status("malformed.pdf") != "FAILED":
        raise AcceptanceFailure("malformed.pdf did not produce a FAILED per-file report")
    if status("unsupported.rtf") != "FAILED":
        raise AcceptanceFailure("unsupported.rtf did not produce a FAILED per-file report")
    for filename in ("handoff-native.pdf", "handoff.docx", "handoff.pptx", "handoff.xlsx"):
        if status(filename) not in {"SUCCEEDED", "WARNING"}:
            raise AcceptanceFailure(f"native parser did not produce usable content for {filename}")
    if status("handoff-scanned.pdf") not in {"WARNING", "SUCCEEDED"}:
        raise AcceptanceFailure("scanned PDF did not produce a persisted OCR outcome")
    searchable = "\n".join(str(block.get("text", "")) for block in blocks)
    if "ORCHID-42" not in searchable or "SP-204" not in searchable:
        raise AcceptanceFailure("parsed business text is missing its fixed native sentinels")
    scan_source_id = _id(sources_by_name["handoff-scanned.pdf"], "scanned PDF source")
    scanned_ocr_blocks = [
        block
        for block in blocks
        if isinstance(block, dict)
        and _pick(block, "sourceRevisionId", "source_revision_id") == scan_source_id
        and isinstance(block.get("locator"), dict)
        and isinstance(block["locator"].get("provenance"), list)
        and any(
            isinstance(item, dict) and item.get("type") == "ocr"
            for item in block["locator"]["provenance"]
        )
    ]
    if not scanned_ocr_blocks:
        raise AcceptanceFailure("scanned PDF has no located OCR-provenance content blocks")
    clear_image_id = _id(sources_by_name["handoff-ocr-clear.png"], "clear OCR source")
    if not any(
        _pick(block, "sourceRevisionId", "source_revision_id") == clear_image_id
        and "ORCHID-42" in str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict)
    ):
        raise AcceptanceFailure("clear image OCR text is missing from located parsed blocks")
    scan = reports_by_name["handoff-scanned.pdf"]
    diagnostics = scan.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        raise AcceptanceFailure("scanned PDF diagnostics must be an array")
    pptx = reports_by_name["handoff.pptx"]
    unsupported = pptx.get("unsupportedContent", pptx.get("unsupported_content", []))
    if not isinstance(unsupported, list) or not unsupported:
        raise AcceptanceFailure("PPTX chart object was not reported as unsupported structure")
    pptx_source_id = _id(sources_by_name["handoff.pptx"], "PPTX source")
    pptx_text = "\n".join(
        str(block.get("text", ""))
        for block in blocks
        if _pick(block, "sourceRevisionId", "source_revision_id") == pptx_source_id
    )
    if "Synthetic presenter note: verify ORCHID-42" not in pptx_text:
        raise AcceptanceFailure("PPTX speaker notes are missing from parsed content")
    xlsx_source_id = _id(sources_by_name["handoff.xlsx"], "XLSX source")
    xlsx_text = "\n".join(
        str(block.get("text", ""))
        for block in blocks
        if _pick(block, "sourceRevisionId", "source_revision_id") == xlsx_source_id
    )
    if "F3: formula =SUM(C3:C4); cached value unavailable" not in xlsx_text:
        raise AcceptanceFailure("XLSX formula/cache evidence is missing from parsed content")
    xlsx_document = json.dumps(reports_by_name["handoff.xlsx"], ensure_ascii=False)
    if "F3" not in xlsx_document and "formula" not in xlsx_document.lower():
        raise AcceptanceFailure("XLSX report lacks the uncached formula diagnostic")
    if MANAGEMENT_SENTINEL in searchable:
        raise AcceptanceFailure("operator-only source metadata leaked into parsed answer blocks")
    return {
        "reportsByName": reports_by_name,
        "blockCount": len(blocks),
        "ocrDiagnosticCount": sum(
            1
            for report in reports
            for row in report.get("diagnostics", [])
            if isinstance(row, dict) and str(_pick(row, "kind")).lower() == "ocr"
        ),
        "scannedPdfOcrBlockCount": len(scanned_ocr_blocks),
    }


def _select_generation_prose_block(
    blocks: list[dict[str, Any]],
    sources_by_name: dict[str, dict[str, Any]],
    reports_by_name: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Choose rich original prose for generation, preferring the fixed TXT source."""

    preferred_sources = (
        "handoff.txt",
        "handoff.md",
        "handoff.docx",
        "handoff-native.pdf",
        "handoff.pptx",
        "handoff.xlsx",
    )
    for filename in preferred_sources:
        source = sources_by_name[filename]
        report = reports_by_name[filename]
        if str(_pick(report, "status")).upper() not in {"SUCCEEDED", "WARNING"}:
            continue
        source_id = _id(source, "prose SourceRevision")
        candidates = [
            block
            for block in blocks
            if _pick(block, "sourceRevisionId", "source_revision_id") == source_id
            and isinstance(block.get("text"), str)
            and len(block["text"].strip()) >= 80
        ]
        candidates = [block for block in candidates if _not_structured_json(block["text"])]
        if candidates:
            chosen = max(candidates, key=lambda block: (len(block["text"]), str(block.get("id"))))
            return chosen, filename
    raise AcceptanceFailure("no rich successful prose block is available for model generation")


def _not_structured_json(text: str) -> bool:
    """Reject JSON blocks because QA must be grounded in extracted prose."""

    try:
        json.loads(text)
    except json.JSONDecodeError:
        return True
    return False


def _assert_knowledge_bundle(
    payload: bytes,
    sources_by_name: dict[str, dict[str, Any]],
    reports_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Verify one Knowledge ZIP covers every successful source and Office evidence."""

    try:
        with ZipFile(BytesIO(payload)) as archive:
            required = {
                "manifest.json",
                "chunks.jsonl",
                "sources.jsonl",
                "hierarchy.json",
                "checksums.json",
            }
            if set(archive.namelist()) != required:
                raise AcceptanceFailure("Knowledge ZIP does not contain its five contract files")
            chunks = [
                json.loads(line)
                for line in archive.read("chunks.jsonl").decode("utf-8").splitlines()
                if line
            ]
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            sources = [
                json.loads(line)
                for line in archive.read("sources.jsonl").decode("utf-8").splitlines()
                if line
            ]
    except (BadZipFile, UnicodeDecodeError, json.JSONDecodeError, KeyError) as error:
        raise AcceptanceFailure("Knowledge export is not a valid readable ZIP") from error
    if (
        not isinstance(manifest, dict)
        or not isinstance(chunks, list)
        or not chunks
        or any(not isinstance(chunk, dict) for chunk in chunks)
        or not isinstance(sources, list)
        or any(not isinstance(source, dict) for source in sources)
    ):
        raise AcceptanceFailure("Knowledge ZIP manifest, sources, or chunks are malformed")
    chunk_source_ids = {
        str(_pick(chunk, "sourceRevisionId", "source_revision_id"))
        for chunk in chunks
        if _pick(chunk, "sourceRevisionId", "source_revision_id") is not None
    }
    source_record_ids = {
        str(_pick(source, "sourceRevisionId", "source_revision_id"))
        for source in sources
        if _pick(source, "sourceRevisionId", "source_revision_id") is not None
    }
    manifest_source_ids = manifest.get("sourceRevisionIds")
    conversion_report = manifest.get("conversionReport")
    empty_text_block_count = (
        conversion_report.get("emptyTextBlockCount")
        if isinstance(conversion_report, dict)
        else None
    )
    if (
        source_record_ids != chunk_source_ids
        or not isinstance(manifest_source_ids, list)
        or set(manifest_source_ids) != chunk_source_ids
        or not isinstance(empty_text_block_count, int)
        or empty_text_block_count < 1
    ):
        raise AcceptanceFailure(
            "Knowledge manifest source coverage or skipped-empty-block count is invalid"
        )
    chunks_by_source = {
        source_id: "\n".join(
            str(_pick(chunk, "text") or "")
            for chunk in chunks
            if str(_pick(chunk, "sourceRevisionId", "source_revision_id")) == source_id
        )
        for source_id in chunk_source_ids
    }
    successful_names = {
        filename
        for filename, report in reports_by_name.items()
        if str(_pick(report, "status")).upper() in {"SUCCEEDED", "WARNING"}
    }
    failed_names = {
        filename
        for filename, report in reports_by_name.items()
        if str(_pick(report, "status")).upper() == "FAILED"
    }
    missing = {
        filename
        for filename in successful_names
        if _id(sources_by_name[filename], "successful Knowledge source") not in chunk_source_ids
    }
    failed_leaks = {
        filename
        for filename in failed_names
        if _id(sources_by_name[filename], "failed Knowledge source") in chunk_source_ids
    }
    if missing or failed_leaks:
        raise AcceptanceFailure(
            "Knowledge chunks do not match successful parse sources "
            f"(missing={sorted(missing)}, failed_sources_included={sorted(failed_leaks)})"
        )
    pptx_id = _id(sources_by_name["handoff.pptx"], "PPTX source")
    xlsx_id = _id(sources_by_name["handoff.xlsx"], "XLSX source")
    if "Synthetic presenter note: verify ORCHID-42" not in chunks_by_source[pptx_id]:
        raise AcceptanceFailure("Knowledge chunks omit readable PPTX speaker notes")
    if "F3: formula =SUM(C3:C4); cached value unavailable" not in chunks_by_source[xlsx_id]:
        raise AcceptanceFailure("Knowledge chunks omit readable XLSX formula/cache evidence")
    return {
        "chunkCount": len(chunks),
        "coveredSuccessfulSources": sorted(successful_names),
        "emptyTextBlockCount": empty_text_block_count,
        "speakerNotesReadable": True,
        "xlsxFormulaEvidenceReadable": True,
    }


def _provider_identity(args: argparse.Namespace, generation: dict[str, Any]) -> dict[str, Any]:
    """Prove the configured binding resolves to the expected real provider identity."""

    provider = _json_object(args.provider_runtime, label="model provider runtime record")
    readiness = _json_object(args.provider_readiness, label="local model readiness record")
    live_readiness = _provider_health(args.provider_health_url)
    ready = provider.get("ready")
    reference = _pick(ready, "connection_ref", "connectionRef") if isinstance(ready, dict) else None
    versions = ready.get("interface_versions", []) if isinstance(ready, dict) else []
    if (
        provider.get("realProvider") is not True
        or provider.get("bindingId") != args.expected_binding_id
        or provider.get("model") != args.expected_model
        or readiness.get("revision") != args.expected_model_revision
        or readiness.get("model") != args.expected_model
        or readiness.get("source_model") != "Qwen/Qwen2.5-1.5B-Instruct"
        or readiness.get("status") != "ready"
        or readiness.get("bind") != "127.0.0.1"
        or readiness.get("generation_requests_made_before_ready") != 0
        or readiness.get("budget", {}).get("max_calls") != 1
        or readiness.get("budget", {}).get("max_output_tokens") != 512
        or live_readiness.get("status") != "ready"
        or live_readiness.get("model") != args.expected_model
        or live_readiness.get("source_model") != "Qwen/Qwen2.5-1.5B-Instruct"
        or live_readiness.get("revision") != args.expected_model_revision
        or live_readiness.get("endpoint") != args.provider_health_url.removesuffix("/healthz")
        or live_readiness.get("budget", {}).get("max_calls") != 1
        or live_readiness.get("budget", {}).get("calls_started") != 0
        or live_readiness.get("budget", {}).get("max_output_tokens") != 512
        or not isinstance(ready, dict)
        or ready.get("capability") != "model.provider.v1"
        or "1" not in versions
        or reference != generation.get("model_endpoint")
        or generation.get("model") != args.expected_model
        or generation.get("max_tokens_per_call") != 512
        or generation.get("timeout_seconds", 180) > 180
        or provider.get("completionCalls") != 0
    ):
        raise AcceptanceFailure(
            "provider identity/config mismatch; require the expected real model binding, "
            "pinned revision, local DirectPluginRuntime ref, zero prior calls, and 512-token cap"
        )
    if re.search(r"fixture|mock|fake|dummy|scripted|test", args.expected_model, re.IGNORECASE):
        raise AcceptanceFailure("fixture/mock model identities cannot be accepted as real provider")
    return {
        "bindingId": args.expected_binding_id,
        "model": args.expected_model,
        "revision": args.expected_model_revision,
        "connectionRef": reference,
        "providerMode": "local-real-model-api-connector",
        "realProvider": True,
    }


def _provider_health(url: str) -> dict[str, Any]:
    """Read local model readiness without sending a completion request."""

    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path != "/healthz"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AcceptanceFailure("provider health URL must be a loopback HTTP /healthz endpoint")
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = response.read()
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise AcceptanceFailure("local model provider readiness endpoint is unavailable") from error
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcceptanceFailure("local model provider returned invalid readiness JSON") from error
    if not isinstance(value, dict):
        raise AcceptanceFailure("local model provider readiness must be a JSON object")
    return value


def _acceptance_status(*, restart_verified: bool) -> str:
    """Label a full restart check PASS and a debug-only run PARTIAL."""

    return "PASS" if restart_verified else "PARTIAL"


def _usage_ledger(path: Path) -> list[dict[str, Any]]:
    """Read provider usage metadata without exposing prompts or completions."""

    if path.is_symlink():
        raise AcceptanceFailure("provider usage ledger must not be a symlink")
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise AcceptanceFailure(f"provider usage ledger row {index} is invalid JSON") from error
        if not isinstance(row, dict):
            raise AcceptanceFailure(f"provider usage ledger row {index} is not an object")
        rows.append(row)
    return rows


def _assert_real_usage(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    model: str,
    revision: str,
) -> dict[str, Any]:
    """Require one completed provider call and a tokenizer-derived usage receipt."""

    if len(before) != 0 or len(after) != 1:
        raise AcceptanceFailure("real provider usage ledger must contain exactly one new request")
    row = after[0]
    usage = row.get("usage")
    if (
        row.get("status") != "completed"
        or row.get("model") != model
        or row.get("revision") != revision
        or not isinstance(row.get("request_id"), str)
        or not row["request_id"].startswith("chatcmpl-local-")
        or not isinstance(usage, dict)
        or not isinstance(usage.get("completion_tokens"), int)
        or not 1 <= usage["completion_tokens"] <= 512
        or not isinstance(row.get("usage_source"), str)
        or not row["usage_source"]
    ):
        raise AcceptanceFailure(
            "provider ledger does not prove one completed capped real-model call"
        )
    return {
        "requestId": row["request_id"],
        "model": row["model"],
        "revision": row["revision"],
        "usage": usage,
        "usageSource": row.get("usage_source"),
    }


def _call_marker(path: Path, data: dict[str, Any]) -> None:
    """Create a durable exclusive marker before submitting the unique paid run."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _validate_resume_marker(
    path: Path,
    *,
    dataset_id: str,
    content_revision_id: str,
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Validate the original one-call admission marker before a safe resume.

    中文：恢复前确认首次生成 admission 的数据集、revision、模型身份和预算一致。
    """

    if path.is_symlink() or not path.is_file():
        raise AcceptanceFailure("original generation-call marker is missing or unsafe")
    marker = _json_object(path, label="original generation-call marker")
    expected = {
        "bindingId": identity["bindingId"],
        "model": identity["model"],
        "modelRevision": identity["revision"],
        "maxCalls": 1,
        "maxExamples": 1,
        "maxOutputTokens": 512,
        "datasetId": dataset_id,
        "contentRevisionId": content_revision_id,
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        raise AcceptanceFailure(
            "original generation-call marker does not match the requested safe resume"
        )
    if not isinstance(marker.get("createdAt"), (int, float)):
        raise AcceptanceFailure("original generation-call marker has no durable admission time")
    return marker


def _resume_live(args: argparse.Namespace) -> dict[str, Any]:
    """Continue one pre-provider rejection on its existing approved Dataset.

    中文：仅在原请求被插件拒绝且 Provider 尚未接收调用时，沿用同一数据集恢复。
    """

    if not args.catalyst_url:
        raise AcceptanceFailure("--catalyst-url is required for live acceptance")
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get(args.token_env):
        raise AcceptanceFailure(
            f"set {args.token_env} in the environment for non-loopback acceptance"
        )
    fixture_root = args.fixture_root.expanduser().absolute()
    manifest = _fixture_manifest(fixture_root)
    files = manifest["files"]
    token = os.environ.get(args.token_env)
    client = ApiClient(args.catalyst_url, token)
    provider_config = _json_object(args.generation_config, label="generation config")
    identity = _provider_identity(args, provider_config)
    usage_before = _usage_ledger(args.provider_usage_ledger)
    if usage_before:
        raise AcceptanceFailure(
            "provider usage ledger is not empty; refusing to spend another call"
        )
    dataset_id = args.resume_dataset_id
    original_marker_path = args.evidence_dir / "generation-call-issued.json"
    retry_marker_path = args.evidence_dir / "generation-resume-issued.json"
    if retry_marker_path.exists() or retry_marker_path.is_symlink():
        raise AcceptanceFailure(
            "generation resume marker already exists; refusing another generateQa admission"
        )
    runtime_path = args.state_dir / "runtime.json"
    launch = _check_mode_and_health(client, runtime_path, args.catalyst_url)
    ui_was_enabled = isinstance(_json_object(runtime_path, label="runtime.json").get("ui"), dict)

    _, _, raw_sources = client.json("GET", f"/api/v1/datasets/{dataset_id}/sources", expected={200})
    if not isinstance(raw_sources, list) or len(raw_sources) != len(files):
        raise AcceptanceFailure("resume Dataset does not contain the fixed 14-source fixture")
    sources_by_name: dict[str, dict[str, Any]] = {}
    for source in raw_sources:
        if not isinstance(source, dict):
            raise AcceptanceFailure("resume Dataset has a malformed source record")
        filename = _pick(source, "filename")
        if filename not in files or filename in sources_by_name:
            raise AcceptanceFailure("resume Dataset source filenames differ from the fixed fixture")
        if (
            _pick(source, "byteLength", "byte_length") != files[filename]["sizeBytes"]
            or _pick(source, "mediaType", "media_type") != files[filename]["mediaType"]
        ):
            raise AcceptanceFailure(f"resume Dataset source metadata changed for {filename}")
        sources_by_name[filename] = source
    if set(sources_by_name) != set(files):
        raise AcceptanceFailure("resume Dataset does not cover every fixed fixture filename")

    _, _, runs = client.json(
        "GET", f"/api/v1/datasets/{dataset_id}/processing-runs", expected={200}
    )
    if not isinstance(runs, list):
        raise AcceptanceFailure("resume Dataset processing runs are not an array")
    parse_runs = [
        row
        for row in runs
        if isinstance(row, dict)
        and str(_pick(row, "operation")).lower() == "parse"
        and str(_pick(row, "state")).upper() == "SUCCEEDED"
    ]
    failed_generation_runs = [
        row
        for row in runs
        if isinstance(row, dict)
        and str(_pick(row, "operation")).lower() == "generateqa"
        and str(_pick(row, "state")).upper() == "FAILED"
    ]
    if len(parse_runs) != 1 or len(failed_generation_runs) != 1:
        raise AcceptanceFailure(
            "safe resume requires exactly one successful parse and one failed generateQa run"
        )
    parse_run = parse_runs[0]
    failed_run = failed_generation_runs[0]
    failed_run_id = _id(failed_run, "previous failed generateQa ProcessingRun")
    failure = failed_run.get("failure")
    if (
        not isinstance(failure, dict)
        or _pick(failure, "code") != "INVALID_REQUEST"
        or _pick(failed_run, "contentRevisionId", "content_revision_id") is None
    ):
        raise AcceptanceFailure(
            "only a persisted pre-provider INVALID_REQUEST generateQa failure is resumable"
        )
    parse_run_id = _id(parse_run, "parse ProcessingRun")
    reports = _wait_parse_reports(
        client, dataset_id, parse_run_id, len(sources_by_name), args.run_timeout
    )
    report_revision_ids = {
        str(_pick(report, "contentRevisionId", "content_revision_id"))
        for report in reports
        if isinstance(report, dict)
        and _pick(report, "contentRevisionId", "content_revision_id") is not None
    }
    if len(report_revision_ids) != 1:
        raise AcceptanceFailure("resume parse reports do not identify one source snapshot")
    source_revision_id = next(iter(report_revision_ids))
    source_revision = _revision(client, dataset_id, source_revision_id)
    if str(_pick(source_revision, "state")).upper() != "APPROVED":
        raise AcceptanceFailure("resume source snapshot is not approved after issue review")
    source_blocks = _revision_blocks(client, source_revision_id)
    parser_evidence = _assert_parser_evidence(reports, sources_by_name, source_blocks)
    successful_source_ids = {
        _id(sources_by_name[name], "successful parse source")
        for name, report in parser_evidence["reportsByName"].items()
        if str(_pick(report, "status")).upper() in {"SUCCEEDED", "WARNING"}
    }

    approved_policy_revision_id = str(_pick(failed_run, "contentRevisionId", "content_revision_id"))
    _validate_resume_marker(
        original_marker_path,
        dataset_id=dataset_id,
        content_revision_id=approved_policy_revision_id,
        identity=identity,
    )
    approved_policy_revision = _revision(client, dataset_id, approved_policy_revision_id)
    if str(_pick(approved_policy_revision, "state")).upper() != "APPROVED":
        raise AcceptanceFailure("previously admitted policy snapshot is not approved")
    policy_blocks = _revision_blocks(client, approved_policy_revision_id)
    if {str(_pick(block, "sourceRevisionId", "source_revision_id")) for block in policy_blocks} != {
        str(_pick(block, "sourceRevisionId", "source_revision_id")) for block in source_blocks
    }:
        raise AcceptanceFailure("approved policy revision changed source block lineage")
    prose_block, generation_source_name = _select_generation_prose_block(
        source_blocks, sources_by_name, parser_evidence["reportsByName"]
    )
    prose_block_id = _id(prose_block, "generation prose ContentBlock")
    prose_source_id = _id(
        sources_by_name[generation_source_name], "generation prose SourceRevision"
    )
    conversation_source_id = _id(
        sources_by_name["source-conversations.jsonl"], "conversation source"
    )
    conversation_blocks = [
        block
        for block in policy_blocks
        if _pick(block, "sourceRevisionId", "source_revision_id") == conversation_source_id
    ]
    if len(conversation_blocks) != 24:
        raise AcceptanceFailure("approved policy revision lost the 24 manual JSONL blocks")
    blocks_to_enable = [
        block
        for block in source_blocks
        if _pick(block, "sourceRevisionId", "source_revision_id") in successful_source_ids
    ]
    if len(blocks_to_enable) != 83:
        raise AcceptanceFailure(
            "approved policy revision does not cover all parsed Knowledge blocks"
        )
    initially_trainable = [
        block
        for block in policy_blocks
        if _pick(block.get("policy", {}), "allowTraining", "allow_training") is True
    ]
    if len(initially_trainable) != 1 or _id(initially_trainable[0]) != prose_block_id:
        raise AcceptanceFailure("resume policy must train only the selected original prose block")
    for block in policy_blocks:
        policy = block.get("policy")
        block_id = _id(block, "approved policy ContentBlock")
        source_id = _pick(block, "sourceRevisionId", "source_revision_id")
        should_know = source_id in successful_source_ids
        should_train = block_id == prose_block_id
        purposes = _pick(policy or {}, "allowedUsePurposes", "allowed_use_purposes", default=[])
        if (
            not isinstance(policy, dict)
            or _pick(policy, "allowKnowledge", "allow_knowledge") is not should_know
            or _pick(policy, "allowTraining", "allow_training") is not should_train
            or ("knowledge_retrieval" in purposes) is not should_know
            or ("model_training" in purposes) is not should_train
        ):
            raise AcceptanceFailure("resume policy scope differs from the one-call acceptance plan")

    queue = _queue(client, dataset_id)
    applicable = [
        row
        for row in queue["items"]
        if isinstance(row, dict)
        and _pick(row, "contentRevisionId", "content_revision_id") == source_revision_id
    ]
    acknowledged_items = [
        row for row in applicable if str(_pick(row, "state")).upper() == "ACKNOWLEDGED"
    ]
    if not applicable or len(acknowledged_items) != len(applicable):
        raise AcceptanceFailure("resume source issues are not all acknowledged")
    if queue["generatedDrafts"]:
        raise AcceptanceFailure("resume Dataset already has generated drafts")
    _, _, versions = client.json(
        "GET", f"/api/v1/datasets/{dataset_id}/data-tools/versions", expected={200}
    )
    if not isinstance(versions, list) or versions:
        raise AcceptanceFailure("resume Dataset already contains a published data-tools version")

    retry_marker = {
        "createdAt": time.time(),
        "resume": True,
        "previousFailedRunId": failed_run_id,
        "bindingId": identity["bindingId"],
        "model": identity["model"],
        "modelRevision": identity["revision"],
        "maxCalls": 1,
        "maxExamples": 1,
        "maxOutputTokens": 512,
        "datasetId": dataset_id,
        "contentRevisionId": approved_policy_revision_id,
    }
    if (
        _usage_ledger(args.provider_usage_ledger)
        or _provider_health(args.provider_health_url).get("budget", {}).get("calls_started") != 0
    ):
        raise AcceptanceFailure(
            "provider use changed during resume preflight; refusing another generateQa admission"
        )
    _call_marker(retry_marker_path, retry_marker)
    generate_admission = _run(
        client,
        dataset_id,
        "generateQa",
        contentRevisionId=approved_policy_revision_id,
        config={
            "generation": {"maxExamples": 1, "maxCalls": 1, "maxOutputTokens": 512},
            "split": {"train": 0.8, "validation": 0.1, "test": 0.1},
        },
    )
    generate_run_id = _id(generate_admission, "resumed generateQa ProcessingRun")
    recovery_evidence = {
        "resumed": True,
        "previousFailedRunId": failed_run_id,
        "previousFailureCode": "INVALID_REQUEST",
        "originalCallMarkerPreserved": True,
        "retryMarker": retry_marker_path.name,
        "usageLedgerWasEmptyBeforeResume": True,
        "providerCallsStartedBeforeResume": 0,
        "datasetId": dataset_id,
        "approvedPolicyRevisionId": approved_policy_revision_id,
    }
    return _finish_after_generation(
        args=args,
        client=client,
        manifest=manifest,
        files=files,
        dataset_id=dataset_id,
        sources_by_name=sources_by_name,
        parser_evidence=parser_evidence,
        source_revision_id=source_revision_id,
        approved_policy_revision_id=approved_policy_revision_id,
        parse_run_id=parse_run_id,
        generation_source_name=generation_source_name,
        prose_block_id=prose_block_id,
        prose_source_id=prose_source_id,
        conversation_source_id=conversation_source_id,
        expected_manual_families=_fixture_source_families(fixture_root),
        blocks_to_enable=blocks_to_enable,
        initially_trainable=initially_trainable,
        applicable=applicable,
        open_items=acknowledged_items,
        blocked_status=409,
        usage_before=usage_before,
        identity=identity,
        launch=launch,
        ui_was_enabled=ui_was_enabled,
        generate_run_id=generate_run_id,
        recovery_evidence=recovery_evidence,
    )


def _assert_sft_bundle(
    payload: bytes,
    *,
    expected_manual_families: set[str] | None = None,
    expected_generated_source_id: str | None = None,
) -> dict[str, int]:
    """Check real learned rows and reject management metadata in the SFT export."""

    try:
        with ZipFile(BytesIO(payload)) as archive:
            names = set(archive.namelist())
            required = {
                "train.jsonl",
                "validation.jsonl",
                "test.jsonl",
                "provenance.jsonl",
                "manifest.json",
            }
            if names != required:
                raise AcceptanceFailure("SFT archive does not contain its five required files")
            all_bytes = b"".join(archive.read(name) for name in sorted(names))
            if MANAGEMENT_SENTINEL.encode("utf-8") in all_bytes:
                raise AcceptanceFailure("operator-only metadata leaked into the SFT package")
            counts: dict[str, int] = {}
            for split in ("train", "validation", "test"):
                rows = [
                    json.loads(line)
                    for line in archive.read(f"{split}.jsonl").decode("utf-8").splitlines()
                    if line
                ]
                counts[split] = len(rows)
                for row in rows:
                    if not isinstance(row, dict):
                        raise AcceptanceFailure("SFT training row is not a JSON object")
                    if set(row) not in (
                        {"conversations"},
                        {"instruction", "output"},
                        {"instruction", "input", "output"},
                    ):
                        raise AcceptanceFailure(
                            "SFT training rows contain unsupported metadata fields"
                        )
                    if "conversations" in row:
                        conversations = row["conversations"]
                        if (
                            not isinstance(conversations, list)
                            or len(conversations) < 2
                            or any(
                                not isinstance(turn, dict)
                                or set(turn) != {"from", "value"}
                                or not isinstance(turn.get("value"), str)
                                or not turn["value"].strip()
                                for turn in conversations
                            )
                        ):
                            raise AcceptanceFailure("SFT conversation row is empty or malformed")
                    elif not all(
                        isinstance(row.get(key), str) and row[key].strip()
                        for key in ("instruction", "output")
                    ):
                        raise AcceptanceFailure("SFT instruction row is missing learned text")
            if sum(counts.values()) == 0:
                raise AcceptanceFailure("SFT artifact has no approved training rows")
            provenance = archive.read("provenance.jsonl").decode("utf-8")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if MANAGEMENT_SENTINEL in provenance:
                raise AcceptanceFailure("operator-only metadata leaked into SFT provenance")
            provenance_rows = [json.loads(line) for line in provenance.splitlines() if line]
            if any(not isinstance(row, dict) for row in provenance_rows):
                raise AcceptanceFailure("SFT provenance contains a non-object row")
            if len(provenance_rows) != sum(counts.values()):
                raise AcceptanceFailure("SFT rows and provenance receipts do not agree")
            families = {
                str(row.get("source_family_id"))
                for row in provenance_rows
                if isinstance(row.get("source_family_id"), str)
            }
            counts["familyCount"] = len(families)
            if expected_manual_families is not None:
                generated_rows = [
                    row for row in provenance_rows if row.get("generation") is not None
                ]
                if (
                    not expected_manual_families.issubset(families)
                    or len(generated_rows) != 1
                    or len(provenance_rows) != len(expected_manual_families) * 2 + 1
                ):
                    raise AcceptanceFailure(
                        "SFT package must contain the 12 manual families and one generated prose family"
                    )
                generated_row = generated_rows[0]
                generated_family = generated_row.get("source_family_id")
                citations = generated_row.get("citations")
                if (
                    not isinstance(generated_family, str)
                    or generated_family in expected_manual_families
                    or not isinstance(citations, list)
                    or not any(
                        isinstance(citation, dict)
                        and citation.get("source_revision_id") == expected_generated_source_id
                        for citation in citations
                    )
                    or families != expected_manual_families | {generated_family}
                ):
                    raise AcceptanceFailure(
                        "SFT generated family does not cite the selected original prose source"
                    )
                split_families = manifest.get("split_stats", {}).get("source_families", {})
                if not isinstance(split_families, dict) or sum(
                    value for value in split_families.values() if isinstance(value, int)
                ) != len(families):
                    raise AcceptanceFailure("SFT manifest family counts do not match provenance")
            return counts
    except (BadZipFile, UnicodeDecodeError, json.JSONDecodeError, KeyError) as error:
        raise AcceptanceFailure("SFT export is not a valid readable package") from error


def _check_mode_and_health(
    client: ApiClient, runtime_path: Path, catalyst_url: str
) -> dict[str, Any]:
    """Verify the launcher really owns a Catalyst-only local runtime."""

    runtime = _json_object(runtime_path, label="runtime.json")
    services = runtime.get("services")
    names = set(services) if isinstance(services, dict) else set()
    if runtime.get("mode") != "catalyst-only" or names != {"catalyst"}:
        raise AcceptanceFailure("launcher runtime is not isolated to Catalyst-only mode")
    service = services["catalyst"]
    if service.get("baseUrl", "").rstrip("/") != catalyst_url.rstrip("/"):
        raise AcceptanceFailure("runtime Catalyst endpoint differs from the verifier endpoint")
    plugin_rows = runtime.get("plugins")
    expected_capabilities = {
        "document.parsing.v1",
        "dataset.knowledge.v1",
        "dataset.generation.v1",
        "dataset.preparation.v1",
    }
    if not isinstance(plugin_rows, list) or len(plugin_rows) != len(expected_capabilities):
        raise AcceptanceFailure("catalyst-only launcher did not start the four required Plugins")
    actual_capabilities: set[str] = set()
    for plugin in plugin_rows:
        event = plugin.get("endpointEvent") if isinstance(plugin, dict) else None
        reference = (
            _pick(plugin, "connection_ref", "connectionRef") if isinstance(plugin, dict) else None
        )
        if (
            not isinstance(plugin, dict)
            or not isinstance(event, dict)
            or event.get("event") != "direct_plugin_ready"
            or not isinstance(event.get("interface_versions"), list)
            or str(_pick(plugin, "capability")) not in expected_capabilities
            or _pick(event, "capability") != _pick(plugin, "capability")
            or _pick(event, "connection_ref", "connectionRef") != reference
            or not isinstance(reference, str)
            or not reference.startswith("grpc://127.0.0.1:")
        ):
            raise AcceptanceFailure(
                "runtime record contains a missing or fabricated Plugin endpoint"
            )
        actual_capabilities.add(str(_pick(plugin, "capability")))
    if actual_capabilities != expected_capabilities:
        raise AcceptanceFailure("Catalyst runtime Plugin capabilities differ from the required set")
    _, _, health = client.json("GET", "/healthz", expected={200})
    return {"mode": runtime["mode"], "health": health, "instanceId": runtime.get("instanceId")}


def _restart_runtime(args: argparse.Namespace, ui_was_enabled: bool) -> str | None:
    """Stop and restart the launcher-owned runtime without touching Product data."""

    launcher = TASK_ROOT / "workspace/packaging/data_tools_trial.py"
    python = Path(sys.executable)
    state_dir = args.state_dir.expanduser().absolute()
    stop = subprocess.run(
        [str(python), str(launcher), "stop", "--state-dir", str(state_dir)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if stop.returncode != 0:
        raise AcceptanceFailure("launcher stop failed before restart")
    command = [
        str(python),
        str(launcher),
        "start",
        "--source-root",
        str(args.source_root),
        "--state-dir",
        str(state_dir),
        "--catalyst-only",
        "--host",
        args.host,
        "--catalyst-port",
        str(args.catalyst_port),
        "--startup-timeout",
        str(args.startup_timeout),
        "--ocr-engine",
        args.ocr_engine,
        "--ocr-languages",
        args.ocr_languages,
        "--ocr-dpi",
        str(args.ocr_dpi),
        "--ocr-minimum-confidence",
        str(args.ocr_minimum_confidence),
        "--generation-config",
        str(args.generation_config),
        "--generation-binding-id",
        args.expected_binding_id,
    ]
    if args.ocr_runtime_root is not None:
        command.extend(("--ocr-runtime-root", str(args.ocr_runtime_root)))
    if args.docling_artifacts_path is not None:
        command.extend(("--docling-artifacts-path", str(args.docling_artifacts_path)))
    if ui_was_enabled:
        navigator_root = args.navigator_root
        if navigator_root is None:
            navigator_root = (
                args.source_root.expanduser().absolute().resolve().parent.parent
                / "Cyrene-Services"
                / "Cyrene-Navigator"
            )
        command.extend(
            (
                "--with-ui",
                "--navigator-root",
                str(navigator_root),
                "--navigator-port",
                str(args.navigator_port),
                "--client-port",
                str(args.client_port),
                "--control-port",
                str(args.control_port),
            )
        )
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        command.append("--allow-remote")
    start = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=max(240, int(args.startup_timeout) + 120),
        env=os.environ.copy(),
    )
    if start.returncode != 0:
        raise AcceptanceFailure("launcher restart failed; see sanitized launcher log files")
    pair_code = None
    match = re.search(r"One-time Navigator pairing code: ([^\s]+)", start.stdout)
    if match:
        pair_code = match.group(1)
    return pair_code


def _finish_after_generation(
    *,
    args: argparse.Namespace,
    client: ApiClient,
    manifest: dict[str, Any],
    files: dict[str, Any],
    dataset_id: str,
    sources_by_name: dict[str, dict[str, Any]],
    parser_evidence: dict[str, Any],
    source_revision_id: str,
    approved_policy_revision_id: str,
    parse_run_id: str,
    generation_source_name: str,
    prose_block_id: str,
    prose_source_id: str,
    conversation_source_id: str,
    expected_manual_families: set[str],
    blocks_to_enable: list[dict[str, Any]],
    initially_trainable: list[dict[str, Any]],
    applicable: list[dict[str, Any]],
    open_items: list[dict[str, Any]],
    blocked_status: int,
    usage_before: list[dict[str, Any]],
    identity: dict[str, Any],
    launch: dict[str, Any],
    ui_was_enabled: bool,
    generate_run_id: str,
    recovery_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the unique generation receipt and finish review/publication/restart."""

    runtime_path = args.state_dir / "runtime.json"
    generate_run = _terminal_run(client, generate_run_id, args.generation_timeout)
    successful_source_ids = {
        _id(sources_by_name[name], "successful parse source")
        for name, report in parser_evidence["reportsByName"].items()
        if str(_pick(report, "status")).upper() in {"SUCCEEDED", "WARNING"}
    }
    after_usage = _usage_ledger(args.provider_usage_ledger)
    provider_usage = _assert_real_usage(
        usage_before,
        after_usage,
        model=args.expected_model,
        revision=args.expected_model_revision,
    )
    stages = generate_run.get("stages", [])
    output = (
        stages[0].get("output")
        if isinstance(stages, list) and stages and isinstance(stages[0], dict)
        else None
    )
    if not isinstance(output, dict):
        raise AcceptanceFailure("successful generateQa run is missing its stage output receipt")
    budget = output.get("budget")
    if (
        output.get("draftCount") != 1
        or not isinstance(budget, dict)
        or budget.get("calls_used") != 1
        or budget.get("provider_completion_tokens") is None
        or budget["provider_completion_tokens"] > 512
        or budget["provider_completion_tokens"] != provider_usage["usage"]["completion_tokens"]
        or output.get("provider", {}).get("binding_id") != identity["bindingId"]
        or output.get("provider", {}).get("model") != identity["model"]
    ):
        raise AcceptanceFailure("generateQa receipt does not prove the exact one-call model budget")
    generated_revision_id = output.get("generatedContentRevisionId")
    if not isinstance(generated_revision_id, str):
        raise AcceptanceFailure("generateQa did not create its draft ContentRevision")
    generated_revision = _revision(client, dataset_id, generated_revision_id)
    if str(_pick(generated_revision, "state")).upper() != "DRAFT":
        raise AcceptanceFailure("generated ContentRevision must remain a human-reviewable DRAFT")
    generated_blocks = _revision_blocks(client, generated_revision_id)
    generated = [
        block for block in generated_blocks if str(_pick(block, "origin")).upper() == "GENERATED"
    ]
    if len(generated) != 1:
        raise AcceptanceFailure("maxExamples=1 did not yield exactly one generated training draft")
    if (
        _pick(generated[0], "sourceRevisionId", "source_revision_id") != prose_source_id
        or _id(generated[0], "generated prose block") == prose_block_id
    ):
        raise AcceptanceFailure("generated QA is not bound to the selected original prose source")
    receipts = [
        block.get("generationReceipt", block.get("generation_receipt")) for block in generated
    ]
    for receipt in receipts:
        receipt_usage = receipt.get("usage") if isinstance(receipt, dict) else None
        completion_tokens = (
            _pick(receipt_usage, "providerCompletionTokens", "provider_completion_tokens")
            if isinstance(receipt_usage, dict)
            else None
        )
        if (
            not isinstance(receipt, dict)
            or _pick(receipt, "bindingId", "binding_id") != identity["bindingId"]
            or _pick(receipt, "model") != identity["model"]
            or _pick(receipt, "sourceBlockIds", "source_block_ids") != [prose_block_id]
            or not isinstance(completion_tokens, int)
            or not 1 <= completion_tokens <= 512
            or completion_tokens != provider_usage["usage"]["completion_tokens"]
        ):
            raise AcceptanceFailure(
                "generated draft receipt does not match the real provider identity"
            )
    if MANAGEMENT_SENTINEL in json.dumps(generated_blocks, ensure_ascii=False):
        raise AcceptanceFailure("operator-only metadata leaked into generated QA content")
    generated_queue = _queue(client, dataset_id)
    if not any(
        isinstance(row, dict)
        and _pick(row, "id", "revisionId") == generated_revision_id
        and str(_pick(row, "state")).upper() == "DRAFT"
        for row in generated_queue["generatedDrafts"]
    ):
        raise AcceptanceFailure("review queue does not expose the generated DRAFT projection")

    draft_restart_evidence: dict[str, Any] = {"skipped": True}
    pair_code = None
    if not args.skip_restart:
        reports_before_draft_restart = _wait_parse_reports(
            client, dataset_id, parse_run_id, len(sources_by_name), args.run_timeout
        )
        draft_restart_before = {
            "generationRun": _canonical_digest(generate_run),
            "generatedBlocks": _canonical_digest(generated_blocks),
            "parseReports": _canonical_digest(reports_before_draft_restart),
            "reviewQueue": _canonical_digest(generated_queue),
        }
        pair_code = _restart_runtime(args, ui_was_enabled)
        _, _, draft_health = client.json("GET", "/healthz", expected={200}, timeout=15)
        draft_runtime = _check_mode_and_health(client, runtime_path, args.catalyst_url)
        run_after_draft_restart = _terminal_run(client, generate_run_id, args.generation_timeout)
        revision_after_draft_restart = _revision(client, dataset_id, generated_revision_id)
        if str(_pick(revision_after_draft_restart, "state")).upper() != "DRAFT":
            raise AcceptanceFailure("generated revision did not remain DRAFT after restart")
        blocks_after_draft_restart = _revision_blocks(client, generated_revision_id)
        reports_after_draft_restart = _wait_parse_reports(
            client, dataset_id, parse_run_id, len(sources_by_name), args.run_timeout
        )
        queue_after_draft_restart = _queue(client, dataset_id)
        queued_draft_after_restart = any(
            isinstance(row, dict)
            and _pick(row, "id", "revisionId") == generated_revision_id
            and str(_pick(row, "state")).upper() == "DRAFT"
            for row in queue_after_draft_restart["generatedDrafts"]
        )
        draft_restart_after = {
            "generationRun": _canonical_digest(run_after_draft_restart),
            "generatedBlocks": _canonical_digest(blocks_after_draft_restart),
            "parseReports": _canonical_digest(reports_after_draft_restart),
            "reviewQueue": _canonical_digest(queue_after_draft_restart),
        }
        if draft_restart_after != draft_restart_before or not queued_draft_after_restart:
            raise AcceptanceFailure(
                "generated DRAFT, receipt, reports, or review queue changed after intermediate restart"
            )
        draft_restart_evidence = {
            "healthy": draft_health,
            "mode": draft_runtime["mode"],
            "stable": draft_restart_after,
            "generatedRevisionState": "DRAFT",
            "generatedDraftInReviewQueue": True,
        }

    blocked_publish_status, _, blocked_publish_body = client.request(
        "POST",
        f"/api/v1/datasets/{dataset_id}/data-tools/versions",
        json_body={
            "contentRevisionId": generated_revision_id,
            "knowledgeRunId": str(uuid.uuid4()),
            "sftRunId": str(uuid.uuid4()),
        },
    )
    if blocked_publish_status != 409:
        raise AcceptanceFailure(
            "publication accepted an unapproved generated draft; "
            f"HTTP {blocked_publish_status}, {_error_detail(blocked_publish_body)}"
        )
    if not args.skip_restart and blocked_publish_status != 409:
        raise AcceptanceFailure("unapproved publication was not blocked after DRAFT restart")
    _, _, approved_generated = client.json(
        "POST",
        f"/api/v1/content-revisions/{generated_revision_id}/review",
        json_body={
            "decision": "APPROVE",
            "note": "Explicit reviewer approval via the human-review API (acceptance operator) of the single generated QA draft.",
        },
        expected={200},
    )
    if str(_pick(approved_generated, "state")).upper() != "APPROVED":
        raise AcceptanceFailure("acceptance-operator review did not approve the generated draft")

    generated_record = json.loads(str(generated[0].get("text", "")))
    if (
        not isinstance(generated_record, dict)
        or set(generated_record) - {"instruction", "input", "output"}
        or not isinstance(generated_record.get("instruction"), str)
        or not generated_record["instruction"].strip()
        or not isinstance(generated_record.get("output"), str)
        or not generated_record["output"].strip()
        or not isinstance(generated_record.get("input", ""), str)
    ):
        raise AcceptanceFailure("generated QA draft is not a valid instruction/output record")
    user_turn = generated_record["instruction"]
    if generated_record.get("input", "").strip():
        user_turn += "\n\n" + generated_record["input"].strip()
    conversation_text = json.dumps(
        {
            "conversations": [
                {"from": "human", "value": user_turn},
                {"from": "gpt", "value": generated_record["output"]},
            ]
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    _, _, edited_generated = client.json(
        "POST",
        f"/api/v1/content-revisions/{generated_revision_id}/blocks/{urllib.parse.quote(_id(generated[0]), safe='')}/edits",
        json_body={
            "expectedRevisionId": generated_revision_id,
            "text": conversation_text,
        },
        expected={201},
    )
    publish_revision_id = _id(edited_generated, "SFT-projected generated ContentRevision")
    if str(_pick(edited_generated, "state")).upper() != "DRAFT":
        raise AcceptanceFailure("projecting generated QA must create a new DRAFT revision")
    post_generation_blocks = _revision_blocks(client, publish_revision_id)
    conversation_blocks_after_generation = [
        block
        for block in post_generation_blocks
        if _pick(block, "sourceRevisionId", "source_revision_id") == conversation_source_id
    ]
    if len(conversation_blocks_after_generation) != 24:
        raise AcceptanceFailure("manual conversation families disappeared after QA generation")
    manual_training_block_ids = {
        _id(block, "conversation ContentBlock") for block in conversation_blocks_after_generation
    }
    for block in conversation_blocks_after_generation:
        block_id = _id(block, "conversation ContentBlock")
        _, _, edited_policy = client.json(
            "POST",
            f"/api/v1/content-revisions/{publish_revision_id}/blocks/{urllib.parse.quote(block_id, safe='')}/edits",
            json_body={
                "expectedRevisionId": publish_revision_id,
                "policy": {
                    "allowKnowledge": True,
                    "allowTraining": True,
                    "allowedPrincipalRefs": ["org:synthetic-itops"],
                    "allowedUsePurposes": ["knowledge_retrieval", "model_training"],
                },
            },
            expected={201},
        )
        publish_revision_id = _id(edited_policy, "manual-family policy ContentRevision")
        if str(_pick(edited_policy, "state")).upper() != "DRAFT":
            raise AcceptanceFailure("JSONL policy edits must create a fresh DRAFT snapshot")

    final_revision = _revision(client, dataset_id, publish_revision_id)
    if str(_pick(final_revision, "state")).upper() != "DRAFT":
        raise AcceptanceFailure("final generated revision must await explicit reviewer approval")
    final_blocks = _revision_blocks(client, publish_revision_id)
    final_generated = next(
        (block for block in final_blocks if _id(block) == _id(generated[0])),
        None,
    )
    if final_generated is None:
        raise AcceptanceFailure("generated prose QA did not survive immutable policy edits")
    final_generation_receipt = _pick(final_generated, "generationReceipt", "generation_receipt")
    if (
        _pick(final_generated, "sourceRevisionId", "source_revision_id") != prose_source_id
        or _pick(final_generated, "origin").upper() != "HUMAN_EDITED"
        or not isinstance(final_generation_receipt, dict)
        or json.loads(str(final_generated.get("text", ""))).get("conversations")
        != [
            {"from": "human", "value": user_turn},
            {"from": "gpt", "value": generated_record["output"]},
        ]
    ):
        raise AcceptanceFailure(
            "generated draft receipt or conversation projection was not preserved"
        )
    expected_training_ids = manual_training_block_ids | {prose_block_id, _id(final_generated)}
    actual_training_ids = {
        _id(block, "final ContentBlock")
        for block in final_blocks
        if _pick(block.get("policy", {}), "allowTraining", "allow_training") is True
    }
    if actual_training_ids != expected_training_ids:
        raise AcceptanceFailure("final SFT policy is broader or narrower than the reviewed fixture")
    for block in final_blocks:
        policy = block.get("policy")
        source_id = _pick(block, "sourceRevisionId", "source_revision_id")
        block_id = _id(block, "final ContentBlock")
        if (
            not isinstance(policy, dict)
            or _pick(policy, "allowKnowledge", "allow_knowledge")
            is not (source_id in successful_source_ids)
            or _pick(policy, "allowTraining", "allow_training")
            is not (block_id in expected_training_ids)
        ):
            raise AcceptanceFailure(
                "final Knowledge/SFT policy differs from the reviewed block set"
            )
    _, _, approved_final_revision = client.json(
        "POST",
        f"/api/v1/content-revisions/{publish_revision_id}/review",
        json_body={
            "decision": "APPROVE",
            "note": "Explicit reviewer approval via the human-review API (acceptance operator) of the generated prose QA and 12 manual conversation families.",
        },
        expected={200},
    )
    if str(_pick(approved_final_revision, "state")).upper() != "APPROVED":
        raise AcceptanceFailure("acceptance-operator review did not approve the final SFT revision")

    knowledge_admission = _run(
        client,
        dataset_id,
        "buildKnowledge",
        contentRevisionId=publish_revision_id,
    )
    sft_admission = _run(
        client,
        dataset_id,
        "prepareSft",
        contentRevisionId=publish_revision_id,
        config={
            "sftMode": "conversation",
            "split": {"train": 0.8, "validation": 0.1, "test": 0.1},
        },
    )
    knowledge_id = _id(knowledge_admission, "knowledge ProcessingRun")
    sft_id = _id(sft_admission, "SFT ProcessingRun")
    _terminal_run(client, knowledge_id, args.run_timeout)
    _terminal_run(client, sft_id, args.run_timeout)
    _, _, version = client.json(
        "POST",
        f"/api/v1/datasets/{dataset_id}/data-tools/versions",
        json_body={
            "contentRevisionId": publish_revision_id,
            "knowledgeRunId": knowledge_id,
            "sftRunId": sft_id,
        },
        expected={201},
    )
    version_id = _id(version, "DatasetVersion")
    data_tools = version.get("dataTools")
    if (
        not isinstance(data_tools, dict)
        or not isinstance(data_tools.get("knowledgeArtifact"), dict)
        or not isinstance(data_tools.get("sftArtifact"), dict)
    ):
        raise AcceptanceFailure("published DatasetVersion does not contain both profile artifacts")
    _, _, knowledge_bytes = client.request(
        "GET",
        f"/api/v1/dataset-versions/{version_id}/data-tools/export?profile=knowledge",
    )
    _, _, sft_bytes = client.request(
        "GET",
        f"/api/v1/dataset-versions/{version_id}/data-tools/export?profile=sft",
    )
    if len(knowledge_bytes) < 100 or MANAGEMENT_SENTINEL.encode("utf-8") in knowledge_bytes:
        raise AcceptanceFailure("knowledge export is empty or contains operator-only metadata")
    knowledge_evidence = _assert_knowledge_bundle(
        knowledge_bytes, sources_by_name, parser_evidence["reportsByName"]
    )
    sft_evidence = _assert_sft_bundle(
        sft_bytes,
        expected_manual_families=expected_manual_families,
        expected_generated_source_id=prose_source_id,
    )

    reports_snapshot = _wait_parse_reports(
        client, dataset_id, parse_run_id, len(sources_by_name), args.run_timeout
    )
    queue_snapshot = _queue(client, dataset_id)
    versions_before = client.json(
        "GET", f"/api/v1/datasets/{dataset_id}/data-tools/versions", expected={200}
    )[2]
    snapshots_before = {
        "reports": _canonical_digest(reports_snapshot),
        "queue": _canonical_digest(queue_snapshot),
        "contentBlocks": _canonical_digest(_revision_blocks(client, publish_revision_id)),
        "versions": _canonical_digest(versions_before),
        "knowledgeExport": hashlib.sha256(knowledge_bytes).hexdigest(),
        "sftExport": hashlib.sha256(sft_bytes).hexdigest(),
    }
    if not args.skip_restart:
        pair_code = _restart_runtime(args, ui_was_enabled) or pair_code
        _, _, health_after = client.json("GET", "/healthz", expected={200}, timeout=15)
        runtime_after = _check_mode_and_health(client, runtime_path, args.catalyst_url)
        reports_after = _wait_parse_reports(
            client, dataset_id, parse_run_id, len(sources_by_name), args.run_timeout
        )
        queue_after = _queue(client, dataset_id)
        versions_after = client.json(
            "GET", f"/api/v1/datasets/{dataset_id}/data-tools/versions", expected={200}
        )[2]
        stable_after = {
            "reports": _canonical_digest(reports_after),
            "queue": _canonical_digest(queue_after),
            "contentBlocks": _canonical_digest(_revision_blocks(client, publish_revision_id)),
            "versions": _canonical_digest(versions_after),
            "knowledgeExport": hashlib.sha256(
                client.request(
                    "GET",
                    f"/api/v1/dataset-versions/{version_id}/data-tools/export?profile=knowledge",
                )[2]
            ).hexdigest(),
            "sftExport": hashlib.sha256(
                client.request(
                    "GET",
                    f"/api/v1/dataset-versions/{version_id}/data-tools/export?profile=sft",
                )[2]
            ).hexdigest(),
        }
        if stable_after != snapshots_before:
            raise AcceptanceFailure(
                "reports, review queue, published version, or package digest changed after restart"
            )
        restart_evidence = {
            "generatedDraftBeforeApproval": draft_restart_evidence,
            "healthy": health_after,
            "mode": runtime_after["mode"],
            "stable": stable_after,
        }
    else:
        restart_evidence = {
            "generatedDraftBeforeApproval": draft_restart_evidence,
            "publishedState": {"skipped": True},
        }

    result = {
        "status": _acceptance_status(restart_verified=not args.skip_restart),
        "fixture": {
            "synthetic": manifest["synthetic"],
            "fileCount": len(files),
            "sha256": _canonical_digest({name: record["sha256"] for name, record in files.items()}),
        },
        "runtime": launch,
        "provider": identity,
        "providerUsage": provider_usage,
        "datasetId": dataset_id,
        "sourceCount": len(sources_by_name),
        "knowledgeEnabledBlockCount": len(blocks_to_enable),
        "preGenerationTrainingBlockCount": len(initially_trainable),
        "parseRunId": parse_run_id,
        "parseReportDigest": snapshots_before["reports"],
        "parser": {
            "blockCount": parser_evidence["blockCount"],
            "ocrDiagnosticCount": parser_evidence["ocrDiagnosticCount"],
            "statuses": {
                name: _pick(row, "status") for name, row in parser_evidence["reportsByName"].items()
            },
        },
        "review": {
            "applicableIssueCount": len(applicable),
            "acknowledgedIssueCount": len(open_items),
            "blockedApprovalHttpStatus": blocked_status,
        },
        "sourceContentRevisionId": source_revision_id,
        "approvedPolicyRevisionId": approved_policy_revision_id,
        "generateQaRunId": generate_run_id,
        "generatedDraftId": generated_revision_id,
        "approvedGeneratedRevisionId": publish_revision_id,
        "generationSourceFilename": generation_source_name,
        "generationSourceBlockId": prose_block_id,
        "generatedDraftCount": 1,
        "blockedUnapprovedPublishHttpStatus": blocked_publish_status,
        "knowledgeRunId": knowledge_id,
        "sftRunId": sft_id,
        "datasetVersionId": version_id,
        "sftSplitRows": {split: sft_evidence[split] for split in ("train", "validation", "test")},
        "sftFamilyCount": sft_evidence["familyCount"],
        "knowledgeEvidence": knowledge_evidence,
        "snapshotsBeforeRestart": snapshots_before,
        "restart": restart_evidence,
    }
    if recovery_evidence is not None:
        result["recovery"] = recovery_evidence
    args.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence_path = args.evidence_dir / "acceptance.json"
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(evidence_path, 0o600)
    if pair_code:
        result["navigatorPairingCode"] = pair_code
    return result


def _run_live(args: argparse.Namespace) -> dict[str, Any]:
    """Exercise the full API workflow and spend exactly one pre-authorized model call."""

    if not args.catalyst_url:
        raise AcceptanceFailure("--catalyst-url is required for live acceptance")
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not os.environ.get(args.token_env):
        raise AcceptanceFailure(
            f"set {args.token_env} in the environment for non-loopback acceptance"
        )
    fixture_root = args.fixture_root.expanduser().absolute()
    manifest = _fixture_manifest(fixture_root)
    files = manifest["files"]
    token = os.environ.get(args.token_env)
    client = ApiClient(args.catalyst_url, token)
    provider_config = _json_object(args.generation_config, label="generation config")
    identity = _provider_identity(args, provider_config)
    usage_before = _usage_ledger(args.provider_usage_ledger)
    if usage_before:
        raise AcceptanceFailure(
            "provider usage ledger is not empty; refusing to spend another call"
        )
    marker_path = args.evidence_dir / "generation-call-issued.json"
    if marker_path.exists() or marker_path.is_symlink():
        raise AcceptanceFailure(
            "unique model-call marker already exists; refusing duplicate generation"
        )
    runtime_path = args.state_dir / "runtime.json"
    launch = _check_mode_and_health(client, runtime_path, args.catalyst_url)
    ui_was_enabled = isinstance(_json_object(runtime_path, label="runtime.json").get("ui"), dict)

    _, _, dataset = client.json(
        "POST",
        "/api/v1/datasets",
        json_body={
            "name": f"Catalyst v0.2 acceptance {uuid.uuid4()}",
            "description": "Synthetic mixed-format parser and review acceptance.",
        },
        expected={201},
    )
    dataset_id = _id(dataset, "Dataset")
    boundary = f"cyrene-v02-{uuid.uuid4().hex}"
    multipart, content_type = _multipart_body(fixture_root, files, boundary)
    _, _, upload_response = client.json(
        "POST",
        f"/api/v1/datasets/{dataset_id}/sources/batch",
        body=multipart,
        content_type=content_type,
        expected={200},
        timeout=120,
    )
    items = upload_response.get("items") if isinstance(upload_response, dict) else None
    if not isinstance(items, list) or len(items) != len(files):
        raise AcceptanceFailure("batch upload did not return one independent result per fixture")
    sources_by_name: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise AcceptanceFailure("batch upload returned a malformed per-file result")
        source = item.get("source")
        error = item.get("error")
        if (source is None) == (error is None) or not isinstance(source, dict):
            raise AcceptanceFailure("fixed fixture upload item did not produce a SourceRevision")
        filename = item.get("filename")
        if filename not in files or filename in sources_by_name:
            raise AcceptanceFailure("batch upload changed or duplicated a fixed fixture filename")
        sources_by_name[filename] = source
    if set(sources_by_name) != set(files):
        raise AcceptanceFailure("batch upload source list differs from fixture manifest")

    parse_order = sorted(
        sources_by_name,
        key=lambda name: name == "source-conversations.jsonl",
    )
    parse_admission = _run(
        client,
        dataset_id,
        "parse",
        sourceRevisionIds=[_id(sources_by_name[name], "SourceRevision") for name in parse_order],
    )
    parse_run_id = _id(parse_admission, "parse ProcessingRun")
    _terminal_run(client, parse_run_id, args.run_timeout)
    reports = _wait_parse_reports(
        client, dataset_id, parse_run_id, len(sources_by_name), args.run_timeout
    )
    report_ids = {
        str(_pick(report, "contentRevisionId", "content_revision_id"))
        for report in reports
        if _pick(report, "contentRevisionId", "content_revision_id") is not None
    }
    if len(report_ids) != 1:
        raise AcceptanceFailure(
            "mixed parse did not persist exactly one successful content snapshot"
        )
    source_revision_id = next(iter(report_ids))
    draft = _revision(client, dataset_id, source_revision_id)
    if str(_pick(draft, "state")).upper() != "DRAFT":
        raise AcceptanceFailure("parsed source snapshot must await human approval")
    draft_blocks = _revision_blocks(client, source_revision_id)
    parser_evidence = _assert_parser_evidence(reports, sources_by_name, draft_blocks)

    queue_before = _queue(client, dataset_id)
    applicable = [
        item
        for item in queue_before["items"]
        if isinstance(item, dict)
        and _pick(item, "contentRevisionId", "content_revision_id") == source_revision_id
    ]
    open_items = [item for item in applicable if str(_pick(item, "state")).upper() == "OPEN"]
    if not open_items:
        raise AcceptanceFailure(
            "parse review queue has no open blocking issue for the content snapshot"
        )
    blocked_status, _, blocked_body = client.request(
        "POST",
        f"/api/v1/content-revisions/{source_revision_id}/review",
        json_body={"decision": "APPROVE", "note": "Acceptance gate probe; expected to be blocked."},
    )
    if blocked_status != 409:
        raise AcceptanceFailure(
            f"unresolved parser/OCR issues did not block approval with HTTP 409 ({blocked_status}); "
            f"{_error_detail(blocked_body)}"
        )
    resolution_note = "Reviewed in the fixed synthetic v0.2 acceptance corpus."
    for item in open_items:
        item_id = _id(item, "ReviewItem")
        _, _, resolved = client.json(
            "POST",
            f"/api/v1/review-items/{item_id}/resolve",
            json_body={"action": "ACKNOWLEDGE", "note": resolution_note},
            expected={200},
        )
        if str(_pick(resolved, "state")).upper() != "ACKNOWLEDGED":
            raise AcceptanceFailure("review issue was not acknowledged")
    queue_after_resolution = _queue(client, dataset_id)
    still_open = [
        item
        for item in queue_after_resolution["items"]
        if isinstance(item, dict)
        and _pick(item, "contentRevisionId", "content_revision_id") == source_revision_id
        and str(_pick(item, "state")).upper() != "ACKNOWLEDGED"
    ]
    if still_open:
        raise AcceptanceFailure("an applicable parser/OCR issue still blocks the source snapshot")
    _, _, approved_sources = client.json(
        "POST",
        f"/api/v1/content-revisions/{source_revision_id}/review",
        json_body={
            "decision": "APPROVE",
            "note": "Approved after resolving parser/OCR review items.",
        },
        expected={200},
    )
    if str(_pick(approved_sources, "state")).upper() != "APPROVED":
        raise AcceptanceFailure("source ContentRevision was not approved after review resolution")

    conversation_source_id = _id(
        sources_by_name["source-conversations.jsonl"], "conversation source"
    )
    expected_manual_families = _fixture_source_families(fixture_root)
    prose_block, generation_source_name = _select_generation_prose_block(
        draft_blocks, sources_by_name, parser_evidence["reportsByName"]
    )
    prose_block_id = _id(prose_block, "generation prose ContentBlock")
    prose_source_id = _id(sources_by_name[generation_source_name], "generation prose source")
    current_revision_id = source_revision_id
    conversation_blocks = [
        block
        for block in draft_blocks
        if _pick(block, "sourceRevisionId", "source_revision_id") == conversation_source_id
    ]
    if len(conversation_blocks) != 24:
        raise AcceptanceFailure(
            "structured conversation source did not produce 24 reviewable records"
        )
    successful_source_ids = {
        _id(sources_by_name[name], "successful parse source")
        for name, report in parser_evidence["reportsByName"].items()
        if str(_pick(report, "status")).upper() in {"SUCCEEDED", "WARNING"}
    }
    blocks_to_enable = [
        block
        for block in draft_blocks
        if _pick(block, "sourceRevisionId", "source_revision_id") in successful_source_ids
    ]
    if not blocks_to_enable:
        raise AcceptanceFailure("successful parse sources have no eligible content blocks")
    for block in blocks_to_enable:
        block_id = _id(block, "ContentBlock")
        allow_training = block_id == prose_block_id
        allowed_purposes = ["knowledge_retrieval"]
        if allow_training:
            allowed_purposes.append("model_training")
        _, _, edited = client.json(
            "POST",
            f"/api/v1/content-revisions/{current_revision_id}/blocks/{urllib.parse.quote(block_id, safe='')}/edits",
            json_body={
                "expectedRevisionId": current_revision_id,
                "policy": {
                    "allowKnowledge": True,
                    "allowTraining": allow_training,
                    "allowedPrincipalRefs": ["org:synthetic-itops"],
                    "allowedUsePurposes": allowed_purposes,
                },
            },
            expected={201},
        )
        current_revision_id = _id(edited, "policy ContentRevision")
        if str(_pick(edited, "state")).upper() != "DRAFT":
            raise AcceptanceFailure(
                "explicit training-policy edits must create a fresh DRAFT snapshot"
            )
    _revision(client, dataset_id, current_revision_id)
    policy_blocks = _revision_blocks(client, current_revision_id)
    manual_blocks_after_policy = [
        block
        for block in policy_blocks
        if _pick(block, "sourceRevisionId", "source_revision_id") == conversation_source_id
    ]
    if len(manual_blocks_after_policy) != 24:
        raise AcceptanceFailure(
            "the approved synthetic conversation lineage lost blocks during edits"
        )
    initially_trainable = [
        block
        for block in policy_blocks
        if _pick(block.get("policy", {}), "allowTraining", "allow_training") is True
    ]
    if len(initially_trainable) != 1 or _id(initially_trainable[0]) != prose_block_id:
        raise AcceptanceFailure(
            "only the selected original prose block may enter initial QA generation"
        )
    for block in policy_blocks:
        policy = block.get("policy")
        block_id = _id(block, "policy ContentBlock")
        source_id = _pick(block, "sourceRevisionId", "source_revision_id")
        should_know = source_id in successful_source_ids
        should_train = block_id == prose_block_id
        if (
            not isinstance(policy, dict)
            or _pick(policy, "allowKnowledge", "allow_knowledge") is not should_know
            or _pick(policy, "allowTraining", "allow_training") is not should_train
            or (
                (
                    "knowledge_retrieval"
                    in _pick(policy, "allowedUsePurposes", "allowed_use_purposes", default=[])
                )
                is not should_know
            )
            or (
                "model_training"
                in _pick(policy, "allowedUsePurposes", "allowed_use_purposes", default=[])
            )
            is not should_train
        ):
            raise AcceptanceFailure("explicit Knowledge/SFT block policy is incorrect")
    policy_blocks_text = json.dumps(policy_blocks, ensure_ascii=False)
    if MANAGEMENT_SENTINEL in policy_blocks_text:
        raise AcceptanceFailure("operator metadata is present in structured content blocks")
    approved_policy_revision_id = current_revision_id
    _, _, approved = client.json(
        "POST",
        f"/api/v1/content-revisions/{current_revision_id}/review",
        json_body={
            "decision": "APPROVE",
            "note": "Explicit reviewer approval via the human-review API (acceptance operator) for the synthetic block-use policy.",
        },
        expected={200},
    )
    if str(_pick(approved, "state")).upper() != "APPROVED":
        raise AcceptanceFailure("policy-reviewed ContentRevision did not become approved")

    _provider_identity(args, provider_config)
    _call_marker(
        marker_path,
        {
            "createdAt": time.time(),
            "bindingId": identity["bindingId"],
            "model": identity["model"],
            "modelRevision": identity["revision"],
            "maxCalls": 1,
            "maxExamples": 1,
            "maxOutputTokens": 512,
            "datasetId": dataset_id,
            "contentRevisionId": current_revision_id,
        },
    )
    generate_admission = _run(
        client,
        dataset_id,
        "generateQa",
        contentRevisionId=current_revision_id,
        config={
            "generation": {"maxExamples": 1, "maxCalls": 1, "maxOutputTokens": 512},
            "split": {"train": 0.8, "validation": 0.1, "test": 0.1},
        },
    )
    generate_run_id = _id(generate_admission, "generateQa ProcessingRun")
    return _finish_after_generation(
        args=args,
        client=client,
        manifest=manifest,
        files=files,
        dataset_id=dataset_id,
        sources_by_name=sources_by_name,
        parser_evidence=parser_evidence,
        source_revision_id=source_revision_id,
        approved_policy_revision_id=approved_policy_revision_id,
        parse_run_id=parse_run_id,
        generation_source_name=generation_source_name,
        prose_block_id=prose_block_id,
        prose_source_id=prose_source_id,
        conversation_source_id=conversation_source_id,
        expected_manual_families=expected_manual_families,
        blocks_to_enable=blocks_to_enable,
        initially_trainable=initially_trainable,
        applicable=applicable,
        open_items=open_items,
        blocked_status=blocked_status,
        usage_before=usage_before,
        identity=identity,
        launch=launch,
        ui_was_enabled=ui_was_enabled,
        generate_run_id=generate_run_id,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build fixture-only and real live-acceptance options."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures-only", action="store_true")
    parser.add_argument("--fixture-root", type=Path, default=FIXTURE_ROOT)
    parser.add_argument("--catalyst-url", default=os.environ.get("CYRENE_CATALYST_V02_URL"))
    parser.add_argument("--token-env", default="CYRENE_DATA_TOOLS_TOKEN")
    parser.add_argument("--source-root", type=Path, default=TASK_ROOT)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--catalyst-port", type=int, default=8024)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--with-ui", action="store_true")
    parser.add_argument("--navigator-root", type=Path)
    parser.add_argument("--navigator-port", type=int, default=8101)
    parser.add_argument("--client-port", type=int, default=5181)
    parser.add_argument("--control-port", type=int, default=5281)
    parser.add_argument("--generation-config", type=Path, default=DEFAULT_GENERATION_CONFIG)
    parser.add_argument(
        "--generation-binding-id",
        dest="expected_binding_id",
        required=False,
        default="catalyst-v02-local-qwen",
    )
    parser.add_argument("--expected-model", default="qwen2.5-1.5b-instruct-local")
    parser.add_argument("--expected-model-revision", default=EXPECTED_QWEN_REVISION)
    parser.add_argument("--provider-runtime", type=Path, default=DEFAULT_PROVIDER_RUNTIME)
    parser.add_argument("--provider-readiness", type=Path, default=DEFAULT_PROVIDER_READINESS)
    parser.add_argument("--provider-health-url", default=DEFAULT_PROVIDER_HEALTH_URL)
    parser.add_argument("--provider-usage-ledger", type=Path, default=DEFAULT_PROVIDER_USAGE)
    parser.add_argument(
        "--evidence-dir", type=Path, default=TASK_ROOT / "reports/catalyst-v02-acceptance"
    )
    parser.add_argument("--ocr-runtime-root", type=Path)
    parser.add_argument(
        "--ocr-engine", choices=("auto", "tesseract-cli", "rapidocr", "none"), default="auto"
    )
    parser.add_argument("--ocr-languages", default="eng,chi_sim")
    parser.add_argument("--ocr-dpi", type=int, default=200)
    parser.add_argument("--ocr-minimum-confidence", type=float, default=0.8)
    parser.add_argument("--docling-artifacts-path", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=90)
    parser.add_argument("--run-timeout", type=float, default=300)
    parser.add_argument("--generation-timeout", type=float, default=240)
    parser.add_argument("--skip-restart", action="store_true")
    parser.add_argument(
        "--resume-dataset-id",
        help="resume one prior INVALID_REQUEST only when no provider completion was used",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run deterministic fixture checks or the explicitly bounded live flow."""

    args = build_parser().parse_args(argv)
    try:
        manifest = _fixture_manifest(args.fixture_root.expanduser().absolute())
        if args.fixtures_only:
            print(
                json.dumps(
                    {"status": "PASS", "synthetic": True, "fixtureCount": len(manifest["files"])},
                    sort_keys=True,
                )
            )
            return 0
        evidence = _resume_live(args) if args.resume_dataset_id else _run_live(args)
    except (AcceptanceFailure, OSError, ValueError, KeyError) as error:
        print(f"CATALYST_V02_ACCEPTANCE_FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
