"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 verify-data-tools-trial.py                                       │
│  Module: scripts.verify_data_tools_trial                            │
│  Role: Verify fixed fixtures and exercise the live trial APIs.       │
│                                                                      │
│  模块职责：检查固定夹具，并调用真实 API 验收试用闭环。                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zipfile import ZipFile

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "data-tools-trial"
PRIVATE_MARKER = "TRIAL_INTERNAL_REVIEW_NOTE_DO_NOT_TRAIN_7f4a"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


class TrialFailure(RuntimeError):
    """Describe one acceptance failure without exposing credentials.

    描述单项验收失败，不包含任何凭据。
    """


class ApiClient:
    """Call one authenticated Product base URL using only the standard library.

    使用 Python 标准库调用一个需要认证的 Product API。
    """

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        expected: set[int] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Send a request and return status, headers and raw body.

        发送请求并返回状态码、响应头和原始响应体。
        """
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request_body = body
        if json_body is not None:
            request_body = json.dumps(
                json_body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path,
            data=request_body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                response_headers = dict(response.headers.items())
                response_body = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            response_headers = dict(error.headers.items())
            response_body = error.read()
        except (urllib.error.URLError, TimeoutError) as error:
            raise TrialFailure(f"{method} {path} could not reach {self.base_url}: {error}") from error
        if expected is not None and status not in expected:
            detail = _safe_error_detail(response_body)
            raise TrialFailure(
                f"{method} {path} returned HTTP {status}; expected {sorted(expected)}; {detail}"
            )
        return status, response_headers, response_body

    def json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        expected: set[int] | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        """Send a request and decode its JSON response.

        发送请求并解码 JSON 响应。
        """
        status, headers, response_body = self.request(
            method,
            path,
            json_body=json_body,
            body=body,
            content_type=content_type,
            expected=expected,
        )
        if not response_body:
            return status, headers, None
        try:
            return status, headers, json.loads(response_body)
        except json.JSONDecodeError as error:
            raise TrialFailure(f"{method} {path} did not return valid JSON") from error


def _safe_error_detail(payload: bytes) -> str:
    """Extract a short code/status message while avoiding request data.

    提取简短错误码或说明，不输出请求数据。
    """
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "non-JSON error response"
    if isinstance(value, dict):
        code = value.get("code") or value.get("type") or "API_ERROR"
        title = value.get("title") or value.get("detail") or value.get("message") or ""
        return f"{code}: {str(title)[:180]}"
    return "API returned an error"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSON Lines and reject non-object records.

    读取 JSON Lines，并拒绝非对象记录。
    """
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise TrialFailure(f"{path.name}:{line_number} is invalid JSON") from error
        if not isinstance(value, dict):
            raise TrialFailure(f"{path.name}:{line_number} is not a JSON object")
        rows.append(value)
    return rows


def verify_fixtures(fixture_root: Path = FIXTURE_ROOT) -> dict[str, int]:
    """Validate deterministic input structure and acceptance expectations.

    校验固定输入的结构和预期验收结果。
    """
    manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
    fixed_files = manifest.get("files")
    if not isinstance(fixed_files, dict) or not fixed_files:
        raise TrialFailure("fixture manifest must pin digests for every fixed input")
    for name, expected_digest in fixed_files.items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(expected_digest, str):
            raise TrialFailure("fixture manifest contains an invalid file receipt")
        if hashlib.sha256((fixture_root / name).read_bytes()).hexdigest() != expected_digest:
            raise TrialFailure(f"fixed fixture digest changed: {name}")
    source = manifest["duplicateSourceScenario"]
    source_path = fixture_root / source["path"]
    source_bytes = source_path.read_bytes()
    digest = hashlib.sha256(source_bytes).hexdigest()
    if digest != source["sha256"]:
        raise TrialFailure("duplicate-source fixture digest does not match manifest")
    policies = [item["policy"] for item in source["uploads"]]
    if len(policies) != 2 or policies[0] == policies[1]:
        raise TrialFailure("duplicate-source fixture must define two different policies")
    if (
        policies[0]["allowKnowledge"] == policies[1]["allowKnowledge"]
        or policies[0]["allowTraining"] == policies[1]["allowTraining"]
    ):
        raise TrialFailure("duplicate-source fixture policies must have opposite output permissions")
    if not source_bytes.startswith(b"%PDF-1.7") or b"/ToUnicode" not in source_bytes:
        raise TrialFailure("PDF fixture is missing its signature or Unicode map")
    if not source_bytes.rstrip().endswith(b"%%EOF"):
        raise TrialFailure("PDF fixture has no EOF marker")

    with ZipFile(fixture_root / "trial-handbook.docx") as archive:
        if archive.testzip() is not None:
            raise TrialFailure("DOCX fixture ZIP is corrupt")
        document = ET.fromstring(archive.read("word/document.xml"))
    docx_text = "".join(node.text or "" for node in document.iter(f"{W_NS}t"))
    if not all(value in docx_text for value in ("中文段落", "🌱🧭", "知识输出", "训练输出")):
        raise TrialFailure("DOCX fixture is missing a required Unicode/table sentinel")
    if len(list(document.iter(f"{W_NS}tbl"))) != 1:
        raise TrialFailure("DOCX fixture must contain exactly one actual table")
    if PRIVATE_MARKER not in docx_text:
        raise TrialFailure("DOCX fixture is missing the private-content review sentinel")

    training = _read_jsonl(fixture_root / "trial-training.jsonl")
    groups = {item.get("sourceFamily") for item in training}
    if len(training) != 20 or len(groups) != 10:
        raise TrialFailure("training fixture must have 20 rows in 10 source groups")
    expected_splits = manifest["training"].get("expectedSplitByFamily")
    if not isinstance(expected_splits, dict) or set(expected_splits) != groups:
        raise TrialFailure("training fixture must pin one deterministic split for each source family")
    if set(expected_splits.values()) != {"train", "validation", "test"}:
        raise TrialFailure("training fixture must deterministically exercise all three splits")
    if len({item.get("sampleId") for item in training}) != len(training):
        raise TrialFailure("training fixture sampleId values must be unique")
    if not all(item.get("_reviewNote") == PRIVATE_MARKER for item in training):
        raise TrialFailure("training fixture is missing its private metadata sentinel")

    echo_rows = _read_jsonl(fixture_root / "echo-reference-actual.jsonl")
    evaluated = [
        row
        for row in echo_rows
        if isinstance(row.get("reference"), str) and isinstance(row.get("actual"), str)
    ]
    skipped = [row for row in echo_rows if row not in evaluated]
    matched = sum(row["reference"] == row["actual"] for row in evaluated)
    if (len(echo_rows), len(evaluated), len(skipped), matched) != (4, 2, 2, 1):
        raise TrialFailure("Echo fixture must yield total=4, evaluated=2, skipped=2, matched=1")
    duplicate_ids = _read_jsonl(fixture_root / "echo-duplicate-sample-id.jsonl")
    if len(duplicate_ids) != 2 or duplicate_ids[0].get("sampleId") != duplicate_ids[1].get("sampleId"):
        raise TrialFailure("Echo duplicate-ID negative fixture must contain two rows with one repeated sampleId")

    malformed_pdf = (fixture_root / "malformed.pdf").read_bytes()
    if not malformed_pdf.startswith(b"%PDF-") or malformed_pdf.rstrip().endswith(b"%%EOF"):
        raise TrialFailure("malformed PDF fixture must have a header but no EOF marker")
    try:
        _read_jsonl(fixture_root / "malformed.jsonl")
    except TrialFailure:
        pass
    else:
        raise TrialFailure("malformed JSONL fixture unexpectedly parsed")
    return {"sources": 4, "trainingRows": len(training), "echoRows": len(echo_rows)}


def _unwrap(value: Any, *keys: str) -> dict[str, Any]:
    """Return a service resource whether the API wrapped it or not.

    兼容 API 直接返回资源或以常见资源键包装的响应。
    """
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, dict):
                return candidate
        return value
    raise TrialFailure("Product API returned a non-object resource")


def _array(value: Any, *keys: str) -> list[dict[str, Any]]:
    """Return a resource list from an array or a documented page wrapper.

    从数组或分页包装响应中取得资源列表。
    """
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = next((value.get(key) for key in keys if isinstance(value.get(key), list)), None)
    else:
        rows = None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TrialFailure("Product API returned an unexpected resource-list shape")
    return rows


def _identifier(value: dict[str, Any], *keys: str) -> str:
    """Read one non-empty identifier from a Product resource.

    从 Product 资源中读取非空标识。
    """
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    raise TrialFailure(f"Product resource is missing identifier field {keys[0]}")


def _health(client: ApiClient) -> None:
    """Require the unauthenticated liveness endpoint to return healthy.

    要求无需认证的存活端点返回健康状态。
    """
    _, _, body = client.json("GET", "/healthz", expected={200})
    if not isinstance(body, dict) or body.get("status") != "ok":
        raise TrialFailure(f"{client.base_url}/healthz did not report status=ok")


def _is_loopback_url(value: str) -> bool:
    """Recognize local-only Product URLs before allowing token-free calls.

    判断 Product URL 是否只绑定到本机，以决定是否允许无令牌调用。
    """
    hostname = urllib.parse.urlsplit(value).hostname
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _require_auth(client: ApiClient) -> None:
    """Prove that unauthenticated API requests fail with HTTP 401.

    验证未认证的 API 请求返回 HTTP 401。
    """
    path = f"/api/v1/datasets/{uuid.uuid4()}"
    status, headers, _ = ApiClient(client.base_url).request("GET", path)
    if status != 401:
        raise TrialFailure(f"unauthenticated GET {path} returned {status}, expected 401")
    if not any(key.lower() == "www-authenticate" for key in headers):
        raise TrialFailure("unauthenticated response is missing WWW-Authenticate")


def _create_dataset(client: ApiClient, name: str) -> dict[str, Any]:
    """Create a trial dataset and return the resource body.

    创建试用 Dataset 并返回资源。
    """
    _, _, body = client.json(
        "POST",
        "/api/v1/datasets",
        json_body={"name": name},
        expected={201},
    )
    return _unwrap(body, "dataset")


def _upload_source(
    client: ApiClient,
    dataset_id: str,
    filename: str,
    payload: bytes,
) -> dict[str, Any]:
    """Upload one immutable source revision as raw bytes.

    以原始字节上传一个不可变来源版本。
    """
    query = urllib.parse.urlencode({"filename": filename})
    _, _, body = client.json(
        "POST",
        f"/api/v1/datasets/{dataset_id}/sources?{query}",
        body=payload,
        content_type="application/octet-stream",
        expected={201},
    )
    return _unwrap(body, "source", "sourceRevision")


def _create_run(
    client: ApiClient,
    dataset_id: str,
    operation: str,
    *,
    source_revision_ids: list[str] | None = None,
    content_revision_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one asynchronous processing run.

    创建一个异步处理运行。
    """
    request: dict[str, Any] = {"operation": operation}
    if source_revision_ids is not None:
        request["sourceRevisionIds"] = source_revision_ids
    if content_revision_id is not None:
        request["contentRevisionId"] = content_revision_id
    if config is not None:
        request["config"] = config
    _, _, body = client.json(
        "POST",
        f"/api/v1/datasets/{dataset_id}/processing-runs",
        json_body=request,
        expected={202},
    )
    return _unwrap(body, "processingRun", "run")


def _wait_run(client: ApiClient, run_id: str, timeout_seconds: int) -> dict[str, Any]:
    """Poll a durable run until it reaches a terminal state.

    轮询持久化运行，直到进入终态。
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _, _, body = client.json(
            "GET",
            f"/api/v1/processing-runs/{run_id}",
            expected={200},
        )
        run = _unwrap(body, "processingRun", "run")
        state = str(run.get("state", "")).upper()
        if state in TERMINAL_RUN_STATES:
            return run
        time.sleep(0.5)
    raise TrialFailure(f"processing run {run_id} did not finish in {timeout_seconds}s")


def _content_revisions(client: ApiClient, dataset_id: str) -> list[dict[str, Any]]:
    """List content revisions newest-first.

    列出从新到旧的内容版本。
    """
    _, _, body = client.json(
        "GET",
        f"/api/v1/datasets/{dataset_id}/content-revisions",
        expected={200},
    )
    return _array(body, "contentRevisions", "items", "revisions")


def _blocks(client: ApiClient, revision_id: str) -> list[dict[str, Any]]:
    """Read every page of content blocks for one revision.

    分页读取一个内容版本的全部 block。
    """
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        _, _, body = client.json(
            "GET",
            f"/api/v1/content-revisions/{revision_id}/blocks?offset={offset}&limit=100",
            expected={200},
        )
        if not isinstance(body, dict):
            raise TrialFailure("content block page is not an object")
        page = body.get("blocks")
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise TrialFailure("content block page has an invalid blocks field")
        result.extend(page)
        total = int(body.get("total", len(result)))
        if len(result) >= total or not page:
            return result
        offset += len(page)


def _latest_revision(client: ApiClient, dataset_id: str) -> dict[str, Any]:
    """Return the newest immutable content revision.

    返回最新的不可变内容版本。
    """
    revisions = _content_revisions(client, dataset_id)
    if not revisions:
        raise TrialFailure("parse run did not create a ContentRevision")
    return max(revisions, key=lambda item: int(item.get("revision", 0)))


def _apply_review_policies(
    client: ApiClient,
    dataset_id: str,
    source_ids: dict[str, str],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Apply per-block purposes, edit one block and approve the resulting revision.

    按 block 设置用途权限，人工编辑一段内容，再批准最终版本。
    """
    duplicate = manifest["duplicateSourceScenario"]["uploads"]
    policy_by_source = {
        source_ids[duplicate[0]["alias"]]: duplicate[0]["policy"],
        source_ids[duplicate[1]["alias"]]: duplicate[1]["policy"],
    }
    default_policy = {
        "allowKnowledge": True,
        "allowTraining": True,
        "allowedPrincipalRefs": ["org:trial-alpha", "org:trial-beta"],
        "allowedUsePurposes": ["knowledge_retrieval", "model_training"],
    }
    private_policy = {
        "allowKnowledge": False,
        "allowTraining": False,
        "allowedPrincipalRefs": [],
        "allowedUsePurposes": [],
    }
    docx_source_id = source_ids["docx"]
    private_marker = manifest["training"]["privateMarker"]
    human_edit_done = False
    for _ in range(500):
        revision = _latest_revision(client, dataset_id)
        revision_id = _identifier(revision, "id", "revisionId")
        blocks = _blocks(client, revision_id)
        if not blocks:
            raise TrialFailure("approved content revision contains no blocks")
        chosen: tuple[dict[str, Any], dict[str, Any], str | None] | None = None
        for block in blocks:
            source_revision_id = str(block.get("sourceRevisionId", ""))
            policy = policy_by_source.get(source_revision_id, default_policy)
            text = str(block.get("text", ""))
            if private_marker in text:
                policy = private_policy
            needs_text_edit = (
                source_revision_id == docx_source_id
                and not human_edit_done
                and "[人工复核]" not in text
            )
            if block.get("policy") != policy or needs_text_edit:
                edited_text = f"{text} [人工复核]" if needs_text_edit else None
                chosen = (block, policy, edited_text)
                break
        if chosen is None:
            break
        block, policy, edited_text = chosen
        request: dict[str, Any] = {
            "expectedRevisionId": revision_id,
            "policy": policy,
        }
        if edited_text is not None:
            request["text"] = edited_text
            human_edit_done = True
        block_id = _identifier(block, "id", "blockId")
        _, _, body = client.json(
            "POST",
            f"/api/v1/content-revisions/{revision_id}/blocks/{block_id}/edits",
            json_body=request,
            expected={201},
        )
        _unwrap(body, "contentRevision", "revision")
    else:
        raise TrialFailure("policy review exceeded 500 immutable revisions")

    approved = _latest_revision(client, dataset_id)
    approved_id = _identifier(approved, "id", "revisionId")
    if not human_edit_done:
        raise TrialFailure("human edit sentinel was not written")
    _, _, body = client.json(
        "POST",
        f"/api/v1/content-revisions/{approved_id}/review",
        json_body={"decision": "APPROVE", "note": "Fixed trial corpus reviewed."},
        expected={200},
    )
    approved = _unwrap(body, "contentRevision", "revision")
    if str(approved.get("state", "")).upper() != "APPROVED":
        raise TrialFailure("content review did not approve the immutable revision")
    return approved, approved_id


def _download_package(
    client: ApiClient,
    version_id: str,
    profile: str,
    destination: Path,
) -> bytes:
    """Download one published package profile and save it for local consumers.

    下载一个已发布的数据包并保存给本地独立消费者使用。
    """
    _, _, body = client.request(
        "GET",
        f"/api/v1/dataset-versions/{version_id}/data-tools/export?"
        + urllib.parse.urlencode({"profile": profile}),
        expected={200},
    )
    if not body.startswith(b"PK"):
        raise TrialFailure(f"{profile} export is not a ZIP package")
    destination.write_bytes(body)
    with ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise TrialFailure(f"{profile} export ZIP is corrupt")
    return body


def _verify_bad_pdf_is_not_published(client: ApiClient, timeout_seconds: int) -> None:
    """Prove malformed PDF input fails before creating approved data or a version.

    验证损坏 PDF 在生成已审核内容或版本前失败。
    """
    dataset = _create_dataset(client, f"Malformed PDF Trial {uuid.uuid4()}")
    dataset_id = _identifier(dataset, "id", "datasetId")
    source = _upload_source(
        client,
        dataset_id,
        "malformed.pdf",
        (FIXTURE_ROOT / "malformed.pdf").read_bytes(),
    )
    run = _create_run(
        client,
        dataset_id,
        "parse",
        source_revision_ids=[_identifier(source, "id", "sourceRevisionId")],
    )
    run = _wait_run(client, _identifier(run, "id", "runId"), timeout_seconds)
    if str(run.get("state", "")).upper() != "FAILED":
        raise TrialFailure("malformed PDF did not produce a failed parse run")
    if _content_revisions(client, dataset_id):
        raise TrialFailure("malformed PDF created a ContentRevision despite failed parsing")


def _assert_stale_after_reapproval(
    client: ApiClient,
    version_id: str,
    content_revision_id: str,
) -> None:
    """Create and approve a later revision, then require the prior version to be stale.

    批准较新的内容版本后，要求此前发布的数据版本显示过期。
    """
    blocks = _blocks(client, content_revision_id)
    if not blocks:
        raise TrialFailure("cannot verify staleness because the approved revision has no blocks")
    selected = next(
        (block for block in blocks if PRIVATE_MARKER not in str(block.get("text", ""))),
        blocks[0],
    )
    _, _, edit_body = client.json(
        "POST",
        f"/api/v1/content-revisions/{content_revision_id}/blocks/"
        f"{_identifier(selected, 'id', 'blockId')}/edits",
        json_body={
            "expectedRevisionId": content_revision_id,
            "text": str(selected.get("text", "")) + " [新批准修订]",
        },
        expected={201},
    )
    revision = _unwrap(edit_body, "contentRevision", "revision")
    revision_id = _identifier(revision, "id", "revisionId")
    _, _, review_body = client.json(
        "POST",
        f"/api/v1/content-revisions/{revision_id}/review",
        json_body={"decision": "APPROVE", "note": "Staleness acceptance revision."},
        expected={200},
    )
    reviewed = _unwrap(review_body, "contentRevision", "revision")
    if str(reviewed.get("state", "")).upper() != "APPROVED":
        raise TrialFailure("follow-up content revision could not be approved for staleness check")
    _, _, version_body = client.json(
        "GET",
        f"/api/v1/dataset-versions/{version_id}",
        expected={200},
    )
    version = _unwrap(version_body, "datasetVersion", "version")
    data_tools = version.get("dataTools")
    if not isinstance(data_tools, dict) or data_tools.get("stale") is not True:
        raise TrialFailure("previously published data-tools version did not become stale")


def _verify_echo_rejects_bad_input(
    client: ApiClient,
    dataset_id: str,
    version_ref: str,
    package_artifact: dict[str, Any],
) -> None:
    """Reject malformed JSONL and duplicate sample IDs without producing a score.

    拒绝损坏 JSONL 和重复 sampleId，且不得生成评测分数。
    """
    for fixture_name in ("malformed.jsonl", "echo-duplicate-sample-id.jsonl"):
        payload = (FIXTURE_ROOT / fixture_name).read_bytes()
        status, _, artifact_body = client.request(
            "POST",
            "/api/v1/session-artifacts",
            body=payload,
            content_type="application/jsonl",
        )
        if status in {400, 422}:
            continue
        if status != 201:
            raise TrialFailure(f"Echo rejected {fixture_name} with unexpected HTTP {status}")
        try:
            artifact = json.loads(artifact_body)
        except json.JSONDecodeError as error:
            raise TrialFailure("Echo session artifact endpoint returned invalid JSON") from error
        invalid_request = {
            "sourceRef": {
                "uri": f"cyrene://catalyst/datasets/{dataset_id}",
                "id": dataset_id,
                "resourceVersion": 1,
            },
            "artifact": artifact,
            "format": "CYRENE_REFERENCE_ACTUAL_JSONL_V1",
            "contentRefs": [version_ref],
            "provenanceRefs": [version_ref],
            "targetDatasetVersion": version_ref,
            "targetPackageArtifact": package_artifact,
        }
        input_status, _, _ = client.request(
            "POST",
            "/api/v1/evaluation-inputs",
            json_body=invalid_request,
        )
        if input_status not in {400, 422}:
            raise TrialFailure(
                f"Echo accepted invalid fixture {fixture_name} with HTTP {input_status}"
            )


def _run_reference_consumer(
    module_dir: Path,
    bundle_path: Path,
    package_digest: str,
    *,
    principal_ref: str,
    expected_allowed_ref: str,
    expected_denied_ref: str,
) -> None:
    """Run the independent KB consumer and check source-level ACL filtering.

    调用独立知识包消费者，并验证按来源执行 ACL 过滤。
    """
    if not module_dir.is_dir():
        raise TrialFailure(f"independent reference consumer is missing: {module_dir}")
    environment = dict(os.environ)
    old_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(module_dir) + (os.pathsep + old_path if old_path else "")
    command = [
        sys.executable,
        "-m",
        "knowledge_reference",
        "search",
        "--bundle",
        str(bundle_path),
        "--package-digest",
        package_digest,
        "--query",
        "ORCHID-42",
        "--principal-ref",
        principal_ref,
        "--use-purpose",
        "knowledge_retrieval",
        "--limit",
        "10",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**environment, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=45,
    )
    if completed.returncode:
        raise TrialFailure(
            "independent KB consumer failed: "
            + (completed.stderr.strip()[-300:] or f"exit {completed.returncode}")
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise TrialFailure("independent KB consumer did not return JSON") from error
    results = response.get("results") if isinstance(response, dict) else None
    if not isinstance(results, list):
        raise TrialFailure("independent KB consumer response is missing results")
    serialized = json.dumps(results, ensure_ascii=False)
    if expected_denied_ref in serialized:
        raise TrialFailure("knowledge consumer leaked a training-only source")
    if expected_allowed_ref not in serialized:
        raise TrialFailure("knowledge consumer did not return the allowed source")


def _check_sft_package(
    package: bytes,
    expected_groups: set[str],
    expected_group_splits: dict[str, str] | None = None,
    expected_group_sample_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    """Check SFT split files, private-field exclusion and group isolation.

    校验 SFT 三个 split、管理字段排除和来源组隔离。
    """
    from collections import Counter
    from io import BytesIO

    rows_by_split: dict[str, list[dict[str, Any]]] = {}
    with ZipFile(BytesIO(package)) as archive:
        names = archive.namelist()
        required = {
            "train.jsonl",
            "validation.jsonl",
            "test.jsonl",
            "provenance.jsonl",
            "manifest.json",
        }
        if not required.issubset(names):
            raise TrialFailure("SFT package is missing its three splits, provenance sidecar, or manifest")
        raw_package = b"".join(archive.read(name) for name in names)
        if PRIVATE_MARKER.encode("utf-8") in raw_package:
            raise TrialFailure("private review marker leaked into the SFT package")
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except json.JSONDecodeError as error:
            raise TrialFailure("SFT manifest is invalid JSON") from error
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "cyrene.sft.bundle.v1":
            raise TrialFailure("SFT manifest has an unsupported schema")
        if manifest.get("split_algorithm") != "sha256-source-family-v1":
            raise TrialFailure("SFT package does not declare the deterministic source-family split algorithm")
        receipts = manifest.get("files")
        if not isinstance(receipts, dict):
            raise TrialFailure("SFT manifest is missing file receipts")
        for split in ("train", "validation", "test"):
            name = f"{split}.jsonl"
            payload = archive.read(name)
            receipt = receipts.get(name)
            if not isinstance(receipt, dict):
                raise TrialFailure(f"SFT manifest is missing the {name} receipt")
            if receipt.get("digest") != "sha256:" + hashlib.sha256(payload).hexdigest():
                raise TrialFailure(f"SFT {name} digest does not match its manifest")
            records: list[dict[str, Any]] = []
            for line_number, line in enumerate(payload.decode("utf-8").splitlines(), 1):
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise TrialFailure(f"SFT {split}:{line_number} is invalid JSON") from error
                if not isinstance(row, dict):
                    raise TrialFailure(f"SFT {split}:{line_number} is not a JSON object")
                if set(row) not in ({"instruction", "output"}, {"instruction", "input", "output"}):
                    raise TrialFailure("SFT answer row contains missing, unknown, or internal fields")
                if not all(isinstance(row.get(key), str) and row[key].strip() for key in ("instruction", "output")):
                    raise TrialFailure("SFT answer row has an empty instruction or output")
                records.append(row)
            if receipt.get("row_count") != len(records):
                raise TrialFailure(f"SFT {name} row count does not match its manifest")
            rows_by_split[split] = records
        sidecar_payload = archive.read("provenance.jsonl")
        sidecar_receipt = receipts.get("provenance.jsonl")
        if not isinstance(sidecar_receipt, dict):
            raise TrialFailure("SFT manifest is missing the provenance receipt")
        if sidecar_receipt.get("digest") != "sha256:" + hashlib.sha256(sidecar_payload).hexdigest():
            raise TrialFailure("SFT provenance sidecar digest does not match its manifest")
        try:
            sidecars = [json.loads(line) for line in sidecar_payload.decode("utf-8").splitlines() if line]
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TrialFailure("SFT provenance sidecar is not valid JSONL") from error
        if any(not isinstance(item, dict) for item in sidecars):
            raise TrialFailure("SFT provenance sidecar contains a non-object row")
        if sidecar_receipt.get("row_count") != len(sidecars):
            raise TrialFailure("SFT provenance row count does not match its manifest")
    total_rows = sum(map(len, rows_by_split.values()))
    if total_rows == 0:
        raise TrialFailure("SFT package contains no records")
    if len(sidecars) != total_rows:
        raise TrialFailure("SFT sidecar count does not match learned split row count")
    groups_by_split = {split: set() for split in ("train", "validation", "test")}
    sidecar_counts: Counter[str] = Counter()
    family_sample_counts: Counter[str] = Counter()
    seen_sample_ids: set[str] = set()
    for index, item in enumerate(sidecars, 1):
        split = item.get("split")
        family = item.get("source_family_id")
        sample_id = item.get("sample_id")
        if split not in groups_by_split or not isinstance(family, str) or not family:
            raise TrialFailure(f"SFT provenance row {index} is missing a valid split or source family")
        if not isinstance(sample_id, str) or not sample_id or sample_id in seen_sample_ids:
            raise TrialFailure("SFT provenance sample IDs must be non-empty and unique")
        if item.get("policy") != {"allow_training": True}:
            raise TrialFailure("SFT sidecar contains a sample without explicit training permission")
        seen_sample_ids.add(sample_id)
        groups_by_split[split].add(family)
        sidecar_counts[split] += 1
        family_sample_counts[family] += 1
    if any(sidecar_counts[split] != len(rows_by_split[split]) for split in groups_by_split):
        raise TrialFailure("SFT sidecar split assignments do not match learned row counts")
    split_stats = manifest.get("split_stats")
    if not isinstance(split_stats, dict) or split_stats.get("algorithm") != manifest["split_algorithm"]:
        raise TrialFailure("SFT manifest is missing the deterministic split statistics")
    if split_stats.get("ratios") != {"train": 0.8, "validation": 0.1, "test": 0.1}:
        raise TrialFailure("SFT package used different split ratios than the fixed trial request")
    sample_stats = split_stats.get("samples")
    family_stats = split_stats.get("source_families")
    if not isinstance(sample_stats, dict) or any(
        sample_stats.get(split) != len(rows_by_split[split]) for split in groups_by_split
    ):
        raise TrialFailure("SFT manifest sample counts do not match split files")
    if not isinstance(family_stats, dict) or any(
        family_stats.get(split) != len(groups_by_split[split]) for split in groups_by_split
    ):
        raise TrialFailure("SFT manifest source-family counts do not match its sidecar")
    all_groups = set().union(*groups_by_split.values())
    if not all_groups:
        raise TrialFailure("SFT sidecar does not expose source-family assignments")
    if expected_groups and not expected_groups.issubset(all_groups):
        raise TrialFailure("SFT package lost one or more source-family identities")
    if expected_group_splits is not None:
        actual_group_splits = {
            family: split
            for split, families in groups_by_split.items()
            for family in families
        }
        if actual_group_splits != expected_group_splits:
            raise TrialFailure("SFT package does not match the fixed deterministic family split")
    if expected_group_sample_counts is not None and dict(family_sample_counts) != expected_group_sample_counts:
        raise TrialFailure("SFT package lost or duplicated one or more fixed training samples")
    split_names = tuple(groups_by_split)
    for index, left in enumerate(split_names):
        for right in split_names[index + 1 :]:
            if groups_by_split[left] & groups_by_split[right]:
                raise TrialFailure(f"SFT source groups leaked between {left} and {right}")
    return {split: len(rows) for split, rows in rows_by_split.items()}


def run_live_acceptance(args: argparse.Namespace) -> None:
    """Exercise both Product flows against deployed services.

    调用已部署服务，端到端验证 Catalyst 双出口和 Echo 报告。
    """
    catalyst_url = args.catalyst_url or os.environ.get("CYRENE_TRIAL_CATALYST_URL")
    echo_url = args.echo_url or os.environ.get("CYRENE_TRIAL_ECHO_URL")
    token_name = args.token_env
    token = os.environ.get(token_name)
    if not catalyst_url or not echo_url:
        raise TrialFailure(
            "set CYRENE_TRIAL_CATALYST_URL and CYRENE_TRIAL_ECHO_URL or pass the corresponding flags"
        )
    if not token and not (_is_loopback_url(catalyst_url) and _is_loopback_url(echo_url)):
        raise TrialFailure(
            f"set {token_name} for non-loopback URLs; never pass the token on the command line"
        )
    catalyst = ApiClient(catalyst_url, token)
    echo = ApiClient(echo_url, token)
    _health(catalyst)
    _health(echo)
    if token:
        _require_auth(catalyst)
        _require_auth(echo)
    _verify_bad_pdf_is_not_published(catalyst, args.run_timeout)

    fixture_manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    dataset = _create_dataset(catalyst, f"Data Tools Trial {uuid.uuid4()}")
    dataset_id = _identifier(dataset, "id", "datasetId")
    if args.require_isolation:
        second_catalyst_url = args.secondary_catalyst_url or os.environ.get(
            "CYRENE_TRIAL_SECONDARY_CATALYST_URL"
        )
        second_echo_url = args.secondary_echo_url or os.environ.get("CYRENE_TRIAL_SECONDARY_ECHO_URL")
        second_token = os.environ.get(args.secondary_token_env)
        if not second_catalyst_url or not second_echo_url or not second_token:
            raise TrialFailure(
                "isolation acceptance requires a second isolated Catalyst/Echo instance and "
                f"{args.secondary_token_env}"
            )
        second_catalyst = ApiClient(second_catalyst_url, second_token)
        second_echo = ApiClient(second_echo_url, second_token)
        _health(second_catalyst)
        _health(second_echo)
        status, _, _ = second_catalyst.request(
            "GET",
            f"/api/v1/datasets/{dataset_id}",
        )
        if status != 404:
            raise TrialFailure(f"cross-workspace Catalyst dataset lookup returned {status}, expected 404")

    pdf_bytes = (FIXTURE_ROOT / "trial-handbook.pdf").read_bytes()
    manifest_duplicate = fixture_manifest["duplicateSourceScenario"]["uploads"]
    first = _upload_source(catalyst, dataset_id, "knowledge-copy.pdf", pdf_bytes)
    second = _upload_source(catalyst, dataset_id, "training-copy.pdf", pdf_bytes)
    if first.get("digest") != second.get("digest"):
        raise TrialFailure("byte-identical source uploads have different digests")
    if first.get("sourceId") == second.get("sourceId"):
        raise TrialFailure("byte-identical uploads were merged into one logical source")
    sources = {
        manifest_duplicate[0]["alias"]: first,
        manifest_duplicate[1]["alias"]: second,
        "docx": _upload_source(
            catalyst,
            dataset_id,
            "trial-handbook.docx",
            (FIXTURE_ROOT / "trial-handbook.docx").read_bytes(),
        ),
        "training": _upload_source(
            catalyst,
            dataset_id,
            "trial-training.jsonl",
            (FIXTURE_ROOT / "trial-training.jsonl").read_bytes(),
        ),
    }
    source_ids = {
        alias: _identifier(source, "id", "sourceRevisionId")
        for alias, source in sources.items()
    }
    parse_run = _create_run(
        catalyst,
        dataset_id,
        "parse",
        source_revision_ids=list(source_ids.values()),
    )
    parse_id = _identifier(parse_run, "id", "runId")
    parse_run = _wait_run(catalyst, parse_id, args.run_timeout)
    if str(parse_run.get("state", "")).upper() != "SUCCEEDED":
        raise TrialFailure(f"fixed trial source parsing failed: {_safe_error_detail(json.dumps(parse_run).encode())}")
    latest = _latest_revision(catalyst, dataset_id)
    latest_id = _identifier(latest, "id", "revisionId")
    blocks = _blocks(catalyst, latest_id)
    extracted_text = "\n".join(str(block.get("text", "")) for block in blocks)
    for sentinel in ("来源键", "ORCHID-42", "中文段落", "🌱🧭", "知识输出", "训练输出"):
        if sentinel not in extracted_text:
            raise TrialFailure(f"parsed content is missing sentinel {sentinel!r}")
    docx_blocks = [
        block
        for block in blocks
        if str(block.get("sourceRevisionId", "")) == source_ids["docx"]
    ]
    table_blocks = [block for block in docx_blocks if str(block.get("kind", "")).casefold() == "table"]
    if not table_blocks:
        raise TrialFailure("DOCX table was flattened or omitted instead of retained as a table block")
    if not any(
        isinstance(block.get("locator"), dict)
        and isinstance(block["locator"].get("tableIndex"), int)
        for block in table_blocks
    ):
        raise TrialFailure("DOCX table block is missing its stable tableIndex locator")
    table_text = "\n".join(str(block.get("text", "")) for block in table_blocks)
    if not all(value in table_text for value in ("恢复代码", "知识输出", "训练输出", "ORCHID-42")):
        raise TrialFailure("DOCX table block did not preserve its row/column text")

    approved, approved_id = _apply_review_policies(
        catalyst,
        dataset_id,
        source_ids,
        fixture_manifest,
    )
    approved_text = "\n".join(
        str(block.get("text", ""))
        for block in _blocks(catalyst, approved_id)
    )
    if "[人工复核]" not in approved_text:
        raise TrialFailure("human-edited content is missing from the approved revision")

    content_revision_id = _identifier(approved, "id", "revisionId")
    knowledge_run = _create_run(
        catalyst,
        dataset_id,
        "buildKnowledge",
        content_revision_id=content_revision_id,
    )
    sft_run = _create_run(
        catalyst,
        dataset_id,
        "prepareSft",
        content_revision_id=content_revision_id,
        config={
            "sftMode": "instruction",
            "split": {"train": 0.8, "validation": 0.1, "test": 0.1},
        },
    )
    knowledge_run = _wait_run(
        catalyst,
        _identifier(knowledge_run, "id", "runId"),
        args.run_timeout,
    )
    sft_run = _wait_run(catalyst, _identifier(sft_run, "id", "runId"), args.run_timeout)
    for name, run in (("knowledge", knowledge_run), ("SFT", sft_run)):
        if str(run.get("state", "")).upper() != "SUCCEEDED":
            raise TrialFailure(f"{name} build run did not succeed")

    _, _, publish_body = catalyst.json(
        "POST",
        f"/api/v1/datasets/{dataset_id}/data-tools/versions",
        json_body={
            "contentRevisionId": content_revision_id,
            "knowledgeRunId": _identifier(knowledge_run, "id", "runId"),
            "sftRunId": _identifier(sft_run, "id", "runId"),
        },
        expected={201},
    )
    version = _unwrap(publish_body, "datasetVersion", "version")
    version_id = _identifier(version, "id", "versionId")
    data_tools = version.get("dataTools")
    if not isinstance(data_tools, dict):
        raise TrialFailure("published DatasetVersion is missing its dataTools projection")
    knowledge_ref = data_tools.get("knowledgeArtifact")
    sft_ref = data_tools.get("sftArtifact")
    if not isinstance(knowledge_ref, dict) or not isinstance(sft_ref, dict):
        raise TrialFailure("published version is missing one of the two output ArtifactRefs")
    knowledge_digest = str(knowledge_ref.get("digest", ""))
    if not knowledge_digest.startswith("sha256:"):
        raise TrialFailure("knowledge ArtifactRef is missing its digest")
    sft_digest = str(sft_ref.get("digest", ""))
    if not sft_digest.startswith("sha256:"):
        raise TrialFailure("SFT ArtifactRef is missing its digest")

    with tempfile.TemporaryDirectory(prefix="cyrene-data-tools-trial-") as temp_root:
        temp = Path(temp_root)
        knowledge_path = temp / "knowledge.zip"
        sft_path = temp / "sft.zip"
        knowledge_bytes = _download_package(catalyst, version_id, "knowledge", knowledge_path)
        sft_bytes = _download_package(catalyst, version_id, "sft", sft_path)
        actual_knowledge_digest = "sha256:" + hashlib.sha256(knowledge_bytes).hexdigest()
        if actual_knowledge_digest != knowledge_digest:
            raise TrialFailure("downloaded knowledge package bytes do not match DatasetVersion ArtifactRef")
        actual_sft_digest = "sha256:" + hashlib.sha256(sft_bytes).hexdigest()
        if actual_sft_digest != sft_digest:
            raise TrialFailure("downloaded SFT package bytes do not match DatasetVersion ArtifactRef")
        training_rows = _read_jsonl(FIXTURE_ROOT / "trial-training.jsonl")
        expected_groups = {str(item["sourceFamily"]) for item in training_rows}
        expected_group_splits = {
            str(family): str(split)
            for family, split in fixture_manifest["training"]["expectedSplitByFamily"].items()
        }
        expected_group_sample_counts = {
            family: sum(row.get("sourceFamily") == family for row in training_rows)
            for family in expected_groups
        }
        sft_counts = _check_sft_package(
            sft_bytes,
            expected_groups,
            expected_group_splits,
            expected_group_sample_counts,
        )
        expected_split_counts = {
            split: sum(value == split for value in expected_group_splits.values())
            * int(fixture_manifest["training"]["recordsPerFamily"])
            for split in ("train", "validation", "test")
        }
        if sft_counts != expected_split_counts:
            raise TrialFailure(f"SFT split counts differ from the fixed group assignment: {sft_counts}")
        if any(sft_counts.get(split, 0) == 0 for split in ("train", "validation", "test")):
            raise TrialFailure("SFT package has an empty train/validation/test split")
        reference_module_dir = (
            Path(args.reference_consumer_dir)
            if args.reference_consumer_dir
            else Path(__file__).resolve().parents[2]
            / "plugins"
            / "plugins"
            / "tools"
            / "knowledge-preparation"
        )
        _run_reference_consumer(
            reference_module_dir,
            knowledge_path,
            knowledge_digest,
            principal_ref="org:trial-alpha",
            expected_allowed_ref=str(first.get("sourceId")),
            expected_denied_ref=str(second.get("sourceId")),
        )

        eval_fixture = (FIXTURE_ROOT / "echo-reference-actual.jsonl").read_bytes()
        _, _, artifact_body = echo.json(
            "POST",
            "/api/v1/session-artifacts",
            body=eval_fixture,
            content_type="application/jsonl",
            expected={201},
        )
        evaluation_artifact = _unwrap(artifact_body, "artifact")
        target_version_ref = f"catalyst://dataset-versions/{version_id}"
        dataset_ref = f"cyrene://catalyst/datasets/{dataset_id}"
        evaluation_input_payload = {
            "sourceRef": {
                "uri": dataset_ref,
                "id": dataset_id,
                "resourceVersion": int(dataset.get("resourceVersion", 1)),
            },
            "artifact": evaluation_artifact,
            "contentRefs": [f"cyrene://catalyst/content-revisions/{content_revision_id}"],
            "provenanceRefs": [target_version_ref],
            "format": "CYRENE_REFERENCE_ACTUAL_JSONL_V1",
            "targetDatasetVersion": target_version_ref,
            "targetPackageArtifact": sft_ref,
        }
        _, _, input_body = echo.json(
            "POST",
            "/api/v1/evaluation-inputs",
            json_body=evaluation_input_payload,
            expected={201},
        )
        evaluation_input = _unwrap(input_body, "evaluationInput", "input")
        input_id = _identifier(evaluation_input, "id", "inputId")
        _, _, suite_body = echo.json(
            "POST",
            "/api/v1/evaluation-suites",
            json_body={
                "name": f"Fixed exact-match trial {uuid.uuid4()}",
                "evaluator": "exact_match.v1",
                "expectedField": "reference",
                "actualField": "actual",
                "threshold": 0.5,
            },
            expected={201},
        )
        suite = _unwrap(suite_body, "evaluationSuite", "suite")
        suite_id = _identifier(suite, "id", "suiteId")
        if args.require_isolation:
            status, _, _ = second_echo.request(
                "GET",
                f"/api/v1/evaluation-inputs/{input_id}",
            )
            if status != 404:
                raise TrialFailure(f"cross-workspace Echo input lookup returned {status}, expected 404")
        _, _, eval_body = echo.json(
            "POST",
            f"/api/v1/evaluation-inputs/{input_id}/actions/evaluate",
            json_body={"suiteId": suite_id, "engineBindingId": "exact-match-plugin"},
            expected={201},
        )
        evaluation = _unwrap(eval_body, "evaluationRun", "run")
        if str(evaluation.get("state", "")).upper() != "SUCCEEDED":
            raise TrialFailure("Echo exact-match evaluation run did not succeed")
        result_id = _identifier(evaluation, "resultId")
        _, _, report_bytes = echo.request(
            "GET",
            f"/api/v1/evaluation-results/{result_id}/export",
            expected={200},
        )
        try:
            report_body = json.loads(report_bytes)
        except json.JSONDecodeError as error:
            raise TrialFailure("Echo report export did not return valid JSON bytes") from error
        if not isinstance(report_body, dict):
            raise TrialFailure("Echo report export did not return a JSON object")
        coverage = report_body.get("coverage", {})
        metrics = report_body.get("metrics", [])
        if report_body.get("schemaVersion") != "cyrene.echo.evaluation-report.v1":
            raise TrialFailure("Echo export has an unexpected report schema")
        if report_body.get("resultId") != result_id:
            raise TrialFailure("Echo report resultId does not match the completed evaluation run")
        if report_body.get("inputDigest") != evaluation_artifact.get("digest"):
            raise TrialFailure("Echo report inputDigest does not match the immutable JSONL artifact")
        if coverage != {"total": 4, "evaluated": 2, "failed": 0, "skipped": 2}:
            raise TrialFailure(f"Echo coverage is incorrect: {coverage}")
        exact_metric = next(
            (metric for metric in metrics if isinstance(metric, dict) and metric.get("name") == "exact_match"),
            None,
        )
        if not exact_metric or exact_metric.get("matched") != 1 or exact_metric.get("value") != 0.5:
            raise TrialFailure("Echo report does not contain the expected exact-match result")
        if exact_metric.get("evaluated") != 2:
            raise TrialFailure("Echo exact-match metric has the wrong denominator")
        if report_body.get("target", {}).get("versionRef") != target_version_ref:
            raise TrialFailure("Echo report target does not match the published DatasetVersion")
        if report_body.get("target", {}).get("packageDigest") != sft_digest:
            raise TrialFailure("Echo report does not bind to the selected package digest")
        if report_body.get("evaluator") != {"id": "exact_match.v1", "version": "1"}:
            raise TrialFailure("Echo report omitted exact-match evaluator provenance")
        report_samples = report_body.get("samples")
        if not isinstance(report_samples, list) or len(report_samples) != 4:
            raise TrialFailure("Echo report must preserve one evidence row per sampleId")
        samples_by_id = {
            item.get("sampleId"): item for item in report_samples if isinstance(item, dict)
        }
        expected_sample_status = {
            "echo-01": ("EVALUATED", True, None),
            "echo-02": ("EVALUATED", False, None),
            "echo-03": ("SKIPPED", None, "MISSING_REFERENCE"),
            "echo-04": ("SKIPPED", None, "MISSING_ACTUAL"),
        }
        for sample_id, (expected_status, expected_match, expected_code) in expected_sample_status.items():
            row = samples_by_id.get(sample_id)
            if not isinstance(row, dict) or row.get("status") != expected_status:
                raise TrialFailure(f"Echo report has incorrect status for {sample_id}")
            if expected_match is not None and row.get("exactMatch") is not expected_match:
                raise TrialFailure(f"Echo report has incorrect exactMatch value for {sample_id}")
            if expected_code is not None and row.get("code") != expected_code:
                raise TrialFailure(f"Echo report has incorrect skip code for {sample_id}")
            if "reference" in row or "actual" in row:
                raise TrialFailure("Echo report repeats private reference or actual answer text")
        if not isinstance(report_body.get("failures"), list) or not isinstance(report_body.get("skips"), list):
            raise TrialFailure("Echo report omitted top-level failures/skips evidence arrays")
        if len(report_body["failures"]) != 0 or len(report_body["skips"]) != 2:
            raise TrialFailure("Echo report has unexpected failed or skipped sample totals")
        for row in _read_jsonl(FIXTURE_ROOT / "echo-reference-actual.jsonl"):
            for field in ("reference", "actual"):
                answer = row.get(field)
                if isinstance(answer, str) and answer in report_bytes.decode("utf-8"):
                    raise TrialFailure("Echo report leaked reference or actual answer text")
        _verify_echo_rejects_bad_input(
            echo,
            dataset_id,
            target_version_ref,
            sft_ref,
        )

        _assert_stale_after_reapproval(
            catalyst,
            version_id,
            content_revision_id,
        )

        if args.include_generation:
            generation = _create_run(
                catalyst,
                dataset_id,
                "generateQa",
                content_revision_id=content_revision_id,
                config={
                    "generation": {
                        "maxExamples": args.generation_max_examples,
                        "maxCalls": args.generation_max_calls,
                    }
                },
            )
            generation = _wait_run(
                catalyst,
                _identifier(generation, "id", "runId"),
                args.run_timeout,
            )
            if str(generation.get("state", "")).upper() != "SUCCEEDED":
                raise TrialFailure("explicit, budgeted grounded QA generation did not succeed")

    print(
        "TRIAL ACCEPTANCE PASS "
        f"(dataset={dataset_id}, version={version_id}, "
        f"outputs=knowledge+sft, echo=exact_match, sft_splits={sft_counts})"
    )


def _available_loopback_port() -> int:
    """Ask the OS for a candidate local API port for an isolated smoke run.

    请求操作系统分配本地端口，供隔离的启动冒烟测试使用。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run_launcher_smoke(timeout_seconds: int) -> None:
    """Start, health-check, inspect, and stop the packaged Linux trial launcher.

    启动、健康检查、核对运行记录并停止 Linux 原生试用启动器。
    """
    task_root = Path(__file__).resolve().parents[2]
    launcher = task_root / "workspace" / "packaging" / "data_tools_trial.py"
    if not launcher.is_file():
        raise TrialFailure(f"native trial launcher is missing: {launcher}")
    first_port = _available_loopback_port()
    second_port = _available_loopback_port()
    while second_port == first_port:
        second_port = _available_loopback_port()
    environment = dict(os.environ)
    environment.pop("CYRENE_DATA_TOOLS_TOKEN", None)
    with tempfile.TemporaryDirectory(prefix="cyrene-data-tools-launcher-smoke-") as root:
        state_dir = Path(root) / "state"
        common = [sys.executable, str(launcher)]
        start = subprocess.run(
            [
                *common,
                "start",
                "--source-root",
                str(task_root),
                "--state-dir",
                str(state_dir),
                "--host",
                "127.0.0.1",
                "--catalyst-port",
                str(first_port),
                "--echo-port",
                str(second_port),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=timeout_seconds,
            cwd=task_root,
        )
        runtime_path = state_dir / "runtime.json"
        started = start.returncode == 0 and runtime_path.is_file()
        try:
            if not started:
                details = (start.stderr or start.stdout).strip()[-400:]
                raise TrialFailure(
                    "native trial launcher did not start cleanly"
                    + (f": {details}" if details else f" (exit {start.returncode})")
                )
            try:
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise TrialFailure("launcher runtime.json could not be read") from error
            if not isinstance(runtime, dict) or runtime.get("status") != "running":
                raise TrialFailure("launcher runtime.json is missing status=running")
            services = runtime.get("services")
            if not isinstance(services, dict):
                raise TrialFailure("launcher runtime.json is missing Product services")
            for product in ("catalyst", "echo"):
                service = services.get(product)
                if not isinstance(service, dict) or not isinstance(service.get("baseUrl"), str):
                    raise TrialFailure(f"launcher runtime.json is missing {product} baseUrl")
                if not _is_loopback_url(service["baseUrl"]):
                    raise TrialFailure(f"launcher smoke unexpectedly bound {product} outside loopback")
                _health(ApiClient(service["baseUrl"]))
            plugin_records = runtime.get("plugins")
            if not isinstance(plugin_records, list):
                raise TrialFailure("launcher runtime.json is missing Plugin readiness records")
            capabilities = {
                item.get("capability")
                for item in plugin_records
                if isinstance(item, dict)
            }
            required_capabilities = {
                "document.parsing.v1",
                "dataset.knowledge.v1",
                "dataset.generation.v1",
                "dataset.preparation.v1",
                "evaluation.runner.v1",
            }
            if capabilities != required_capabilities:
                raise TrialFailure("launcher did not start the five required direct Plugin capabilities")
            for item in plugin_records:
                reference = urllib.parse.urlsplit(str(item.get("connection_ref", "")))
                if reference.scheme != "grpc" or reference.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    raise TrialFailure("launcher recorded a Plugin endpoint outside loopback")
            status = subprocess.run(
                [*common, "status", "--state-dir", str(state_dir)],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=20,
                cwd=task_root,
            )
            if status.returncode != 0 or json.loads(status.stdout).get("status") != "running":
                raise TrialFailure("launcher status command did not report a healthy trial")
        finally:
            if runtime_path.is_file():
                stopped = subprocess.run(
                    [*common, "stop", "--state-dir", str(state_dir)],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                    timeout=30,
                    cwd=task_root,
                )
                if stopped.returncode != 0:
                    raise TrialFailure("native trial launcher could not stop its smoke instance")
    print("LAUNCHER SMOKE PASS (Catalyst + Echo + five loopback Plugins; stopped cleanly)")


def _parser() -> argparse.ArgumentParser:
    """Build the bounded verifier command line.

    构建范围受控的验收命令行。
    """
    parser = argparse.ArgumentParser(description="Verify the fixed Catalyst/Echo data-tools trial.")
    parser.add_argument("--fixtures-only", action="store_true", help="Check only the synthetic fixed corpus.")
    parser.add_argument(
        "--launcher-smoke",
        action="store_true",
        help="Start and stop the native local launcher in temporary state, then check real health and Plugin readiness.",
    )
    parser.add_argument("--launcher-timeout", type=int, default=600)
    parser.add_argument("--catalyst-url", help="Catalyst API root; defaults to CYRENE_TRIAL_CATALYST_URL.")
    parser.add_argument("--echo-url", help="Echo API root; defaults to CYRENE_TRIAL_ECHO_URL.")
    parser.add_argument(
        "--token-env",
        default="CYRENE_DATA_TOOLS_TOKEN",
        help="Environment variable containing the primary bearer token.",
    )
    parser.add_argument("--require-isolation", action="store_true", help="Require a second isolated organization instance.")
    parser.add_argument("--secondary-catalyst-url")
    parser.add_argument("--secondary-echo-url")
    parser.add_argument("--secondary-token-env", default="CYRENE_TRIAL_SECONDARY_TOKEN")
    parser.add_argument("--reference-consumer-dir", help="Directory containing knowledge_reference.py/module.")
    parser.add_argument("--run-timeout", type=int, default=180)
    parser.add_argument("--include-generation", action="store_true", help="Explicitly opt in to bounded model calls.")
    parser.add_argument("--generation-max-examples", type=int, default=4)
    parser.add_argument("--generation-max-calls", type=int, default=1)
    return parser


def main() -> int:
    """Run local fixture checks and, unless requested otherwise, live APIs.

    执行本地夹具检查；除非指定仅查夹具，否则继续验收真实 API。
    """
    args = _parser().parse_args()
    try:
        counts = verify_fixtures()
        print(f"FIXTURES PASS (sources={counts['sources']}, training={counts['trainingRows']}, echo={counts['echoRows']})")
        if args.launcher_smoke:
            if args.launcher_timeout < 1:
                raise TrialFailure("--launcher-timeout must be positive")
            run_launcher_smoke(args.launcher_timeout)
        elif not args.fixtures_only:
            if args.run_timeout < 1:
                raise TrialFailure("--run-timeout must be positive")
            if args.include_generation and (args.generation_max_examples < 1 or args.generation_max_calls < 1):
                raise TrialFailure("generation budgets must be positive")
            run_live_acceptance(args)
    except (TrialFailure, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"TRIAL ACCEPTANCE BLOCKED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
