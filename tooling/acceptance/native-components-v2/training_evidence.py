"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 training_evidence.py                                            │
│  Module: native-components-v2.training_evidence                     │
│  Role: Offline verification of explicit Yield and Kernel evidence.  │
│                                                                      │
│  模块职责：离线核对显式提供的 Yield 与 Kernel 训练证据。              │
└─────────────────────────────────────────────────────────────────────┘

This module consumes captured responses and owner receipts only. It does not
contact Product, Kernel, Relay, a host, or a GPU. A verified training result
never upgrades missing independent Kernel lease-release evidence.

本模块只消费已采集的响应与 owner 回执，不连接 Product、Kernel、Relay、主机或 GPU。
训练结果已核验不代表缺失的 Kernel 独立租约释放证据已核验。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TERMINAL_LEASE_STATES = {
    "LEASE_STATE_RELEASED",
    "LEASE_STATE_EXPIRED",
    "LEASE_STATE_REVOKED",
}
_ACTIVE_TASK_STATES = {
    "WORKSPACE_TASK_ACTIVITY_STATE_ACCEPTED",
    "WORKSPACE_TASK_ACTIVITY_STATE_QUEUED",
    "WORKSPACE_TASK_ACTIVITY_STATE_DISPATCHING",
    "WORKSPACE_TASK_ACTIVITY_STATE_RUNNING",
    "WORKSPACE_TASK_ACTIVITY_STATE_CANCELING",
    "WORKSPACE_TASK_ACTIVITY_STATE_INFLIGHT",
    "ACCEPTED",
    "QUEUED",
    "DISPATCHING",
    "RUNNING",
    "CANCELING",
    "INFLIGHT",
}
_PHASES = {
    "tiny": "training-tiny-dry-run",
    "formal": "training-execution",
}


def verify_training_attempt(
    *,
    phase: str,
    run: dict[str, Any],
    attempts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    execution_receipt: dict[str, Any],
    kernel_readiness: list[dict[str, Any]],
    release_response: dict[str, Any] | None = None,
    artifact_manifest_path: str | Path,
    expected_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify one captured tiny or formal attempt without asserting full acceptance.

    The Product attempt identifier is derived from Yield's documented UUIDv5
    projection and the raw control-plane attempt ID in the receipt's product
    spec. Kernel activity proves only the matching task was observed. The owner
    receipt can prove its own terminal release response, not independent Kernel
    resource state.

    离线核验一个已采集的 tiny 或 formal attempt，不直接给出完整验收结论。
    Kernel activity 只证明观察到对应 task；owner 回执不能替代 Kernel 独立资源状态。

    Args:
        phase: ``tiny`` or ``formal``.
        run: One actual scoped Yield run response.
        attempts: The actual attempt projection for that same run.
        events: Captured Yield event projections for that run.
        execution_receipt: The owner-written Kernel executor receipt.
        kernel_readiness: Captured Kernel readiness snapshots.
        artifact_manifest_path: CAS path to a portable directory manifest V2.
        expected_parameters: Optional override for the formal run's expected values.
    Returns:
        A JSON-serializable evidence report. Missing source evidence is UNKNOWN;
        contradictory evidence is NOT_VERIFIED.
    """
    report: dict[str, Any] = {
        "phase": phase,
        "training_status": "UNKNOWN",
        "acceptance_status": "NOT_VERIFIED",
        "owner_release_status": "UNKNOWN",
        "kernel_resource_release_status": "UNKNOWN",
        "checks": {},
        "verified": {},
        "blockers": [],
    }
    if phase not in _PHASES:
        return _blocked(report, "NOT_VERIFIED", "unsupported_phase")

    run_id = _identifier(run, "id")
    draft_id = _identifier(run, "draftId", "draft_id")
    state = _value(run, "state")
    report["verified"]["run_id"] = run_id if run_id else None
    report["verified"]["draft_id"] = draft_id if draft_id else None
    if not run_id or not draft_id:
        _blocked(report, "UNKNOWN", "run_identity_missing")
    elif state != "COMPLETED":
        _blocked(
            report,
            "NOT_VERIFIED" if state in {"FAILED", "CANCELLED"} else "UNKNOWN",
            "run_not_completed",
        )
    else:
        report["checks"]["run_terminal_success"] = "VERIFIED"

    attempt_id, raw_attempt_id = _matching_attempt(run_id, phase, attempts, execution_receipt)
    report["verified"]["attempt_id"] = attempt_id
    if attempt_id is None or raw_attempt_id is None:
        _blocked(report, "UNKNOWN", "attempt_to_receipt_link_missing")
    else:
        report["checks"]["attempt_to_receipt_link"] = "VERIFIED"

    matched_events = _matching_events(run_id, raw_attempt_id, phase, events)
    if matched_events is None:
        _blocked(report, "UNKNOWN", "run_events_missing_or_mismatched")
    else:
        observed_steps = sorted(
            {
                step
                for event in matched_events
                if (step := _integer(_value(event, "step"))) is not None
            }
        )
        report["verified"]["observed_event_steps"] = observed_steps
        report["checks"]["event_link"] = "VERIFIED"

    task_observed = _matching_active_task(draft_id, kernel_readiness)
    if task_observed:
        report["checks"]["kernel_task_observed"] = "VERIFIED"
        report["verified"]["kernel_task_id"] = draft_id
    else:
        _blocked(report, "UNKNOWN", "matching_kernel_task_not_observed")

    worker_lease = _verify_worker_lease(execution_receipt)
    if worker_lease:
        report["checks"]["owner_worker_lease_allocation"] = "VERIFIED"
        report["verified"].update(worker_lease)
    else:
        _blocked(report, "UNKNOWN", "owner_worker_lease_allocation_link_missing")

    release_status, release_id = _owner_release(execution_receipt, release_response)
    report["owner_release_status"] = release_status
    if release_status == "OBSERVED":
        report["checks"]["owner_release_response"] = "VERIFIED"
        report["verified"]["released_lease_id"] = release_id
    elif release_status == "NOT_VERIFIED":
        _blocked(report, "NOT_VERIFIED", "owner_release_failed_or_mismatched")
    else:
        _blocked(report, "UNKNOWN", "owner_release_response_missing")
    # No current Kernel RPC returns an exact lease readback to this collector.
    report["kernel_resource_release_status"] = "UNKNOWN"

    parameter_status = _verify_parameters(phase, run, execution_receipt, expected_parameters)
    if parameter_status is True:
        report["checks"]["expected_parameters"] = "VERIFIED"
    elif parameter_status is False:
        _blocked(report, "NOT_VERIFIED", "parameters_mismatch")
    else:
        _blocked(report, "UNKNOWN", "parameters_not_exposed_or_missing")

    output_status = _verify_result_artifacts(run)
    if output_status is True:
        report["checks"]["result_artifact_summary"] = "VERIFIED"
    elif output_status is False:
        _blocked(report, "NOT_VERIFIED", "result_artifact_summary_mismatch")
    else:
        _blocked(report, "UNKNOWN", "result_artifact_summary_missing")

    artifact_status, artifact_evidence = _verify_artifact_manifest(run, artifact_manifest_path)
    if artifact_status == "VERIFIED":
        report["checks"]["artifact_manifest"] = "VERIFIED"
        report["verified"].update(artifact_evidence)
    elif artifact_status == "NOT_VERIFIED":
        _blocked(report, "NOT_VERIFIED", "artifact_manifest_mismatch")
    else:
        _blocked(report, "UNKNOWN", "artifact_manifest_missing")

    optimizer_steps = (
        _optimizer_steps_from_manifest(artifact_manifest_path)
        if artifact_status == "VERIFIED"
        else None
    )
    if optimizer_steps == 1:
        report["checks"]["completed_optimizer_steps"] = "VERIFIED"
        report["verified"]["completed_optimizer_steps"] = 1
    elif optimizer_steps is None and artifact_status == "VERIFIED":
        _blocked(report, "UNKNOWN", "trainer_state_global_step_missing")
    elif optimizer_steps is not None:
        _blocked(report, "NOT_VERIFIED", "trainer_state_global_step_mismatch")

    if not report["blockers"]:
        report["training_status"] = "VERIFIED"
    elif any(item["status"] == "NOT_VERIFIED" for item in report["blockers"]):
        report["training_status"] = "NOT_VERIFIED"
    else:
        report["training_status"] = "UNKNOWN"
    return report


def verify_training_pair(*, tiny: dict[str, Any], formal: dict[str, Any]) -> dict[str, Any]:
    """Keep tiny and formal evidence distinct and refuse shared identities.

    保持 tiny 与 formal 证据独立，拒绝复用 attempt、worker 或 lease 身份。
    """
    tiny_report = verify_training_attempt(phase="tiny", **tiny)
    formal_report = verify_training_attempt(phase="formal", **formal)
    tiny_ids = tiny_report["verified"]
    formal_ids = formal_report["verified"]
    same_run = (
        tiny_ids.get("run_id") is not None
        and tiny_ids.get("run_id") == formal_ids.get("run_id")
        and tiny_ids.get("draft_id") == formal_ids.get("draft_id")
    )
    shared = any(
        tiny_ids.get(key) and tiny_ids.get(key) == formal_ids.get(key)
        for key in ("attempt_id", "worker_id", "lease_id")
    )
    if shared or not same_run:
        for report in (tiny_report, formal_report):
            _blocked(
                report,
                "NOT_VERIFIED",
                "tiny_formal_identity_reused" if shared else "tiny_formal_run_mismatch",
            )
            report["training_status"] = "NOT_VERIFIED"
    return {
        "tiny": tiny_report,
        "formal": formal_report,
        "training_status": "VERIFIED"
        if tiny_report["training_status"] == "VERIFIED"
        and formal_report["training_status"] == "VERIFIED"
        else "NOT_VERIFIED"
        if "NOT_VERIFIED" in {tiny_report["training_status"], formal_report["training_status"]}
        else "UNKNOWN",
        "acceptance_status": "NOT_VERIFIED",
        "kernel_resource_release_status": "UNKNOWN",
    }


def _matching_attempt(
    run_id: str | None,
    phase: str,
    attempts: list[dict[str, Any]],
    receipt: dict[str, Any],
) -> tuple[str | None, str | None]:
    if not run_id:
        return None, None
    launch = _mapping(_value(receipt, "launch"))
    launch_extra = _mapping(_value(launch, "extra"))
    product_spec = _mapping(_value(launch_extra, "product_spec", "productSpec"))
    raw_id = _identifier(product_spec, "job_id", "jobId")
    runtime_attempt_id = _identifier(launch_extra, "attempt_id", "attemptId")
    if not raw_id or not runtime_attempt_id or not runtime_attempt_id.startswith(raw_id + ":"):
        return None, raw_id
    attempt_uri = f"cyrene://yield/training-runs/{run_id}/attempts/{raw_id}"
    projected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, attempt_uri))
    match = next(
        (
            item
            for item in attempts
            if _identifier(item, "id") == projected_id
            and _value(item, "trainingRunId", "training_run_id") == run_id
            and _value(item, "phase") == _PHASES[phase]
            and _value(item, "state") == "SUCCEEDED"
        ),
        None,
    )
    return (projected_id, raw_id) if match is not None else (None, raw_id)


def _matching_events(
    run_id: str | None,
    raw_attempt_id: str | None,
    phase: str,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    if not run_id or not raw_attempt_id:
        return None
    matched = [
        item
        for item in events
        if _value(item, "trainingRunId", "training_run_id") == run_id
        and _value(item, "attemptId", "attempt_id") == raw_attempt_id
        and _value(item, "phase") == _PHASES[phase]
    ]
    return matched or None


def _matching_active_task(draft_id: str | None, readiness: list[dict[str, Any]]) -> bool:
    if not draft_id:
        return False
    for response in readiness:
        tasks = _value(response, "activeTasks", "active_tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            state = str(_value(task, "state") or "")
            if (
                _value(task, "sourceId", "source_id") == "cyrene-yield"
                and _value(task, "taskId", "task_id") == draft_id
                and state in _ACTIVE_TASK_STATES
            ):
                return True
    return False


def _verify_worker_lease(receipt: dict[str, Any]) -> dict[str, Any] | None:
    worker = _mapping(_value(receipt, "worker"))
    lease = _mapping(_value(receipt, "lease"))
    lease_identity = _mapping(_value(lease, "identity"))
    holder = _mapping(_value(lease, "holder"))
    resources = _value(lease, "resources")
    operation = _mapping(_value(receipt, "operation"))
    operation_identity = _mapping(_value(operation, "identity"))
    metadata = _mapping(_value(operation, "metadata"))
    worker_id = _identifier(worker, "id")
    lease_id = _identifier(lease_identity, "id")
    resource_ids = (
        sorted(
            str(_value(_mapping(item), "id"))
            for item in resources
            if isinstance(item, dict) and _identifier(item, "id")
        )
        if isinstance(resources, list)
        else []
    )
    if not worker_id or not lease_id or not resource_ids:
        return None
    if _identifier(holder, "id") != worker_id:
        return None
    if _value(metadata, "worker.id", "worker_id") != worker_id:
        return None
    if _identifier(operation_identity, "id") != f"operation/start/{worker_id}":
        return None
    if _identifier(_mapping(_value(operation, "parent")), "id") not in {None, lease_id}:
        return None
    return {"worker_id": worker_id, "lease_id": lease_id, "resource_ids": resource_ids}


def _owner_release(
    receipt: dict[str, Any], response: dict[str, Any] | None
) -> tuple[str, str | None]:
    if _value(receipt, "released") is not True:
        return (
            ("UNKNOWN", None)
            if _value(receipt, "releaseReceipt") is None
            else ("NOT_VERIFIED", None)
        )
    lease = _mapping(_value(receipt, "lease"))
    receipt_release = _mapping(_value(receipt, "releaseReceipt", "release_receipt"))
    release = response if isinstance(response, dict) else receipt_release
    if isinstance(response, dict) and receipt_release and response != receipt_release:
        return "NOT_VERIFIED", _identifier(_mapping(_value(lease, "identity")), "id")
    lease_identity = _mapping(_value(lease, "identity"))
    release_identity = _mapping(_value(release, "identity"))
    lease_id = _identifier(lease_identity, "id")
    if (
        not lease_id
        or _identifier(release_identity, "id") != lease_id
        or _value(release, "state") not in _TERMINAL_LEASE_STATES
    ):
        return "NOT_VERIFIED", lease_id
    release_resources = _value(release, "resources")
    acquired_resources = _value(lease, "resources")
    if isinstance(release_resources, list) and isinstance(acquired_resources, list):
        left = sorted(_identifier(_mapping(item), "id") for item in release_resources)
        right = sorted(_identifier(_mapping(item), "id") for item in acquired_resources)
        if left != right:
            return "NOT_VERIFIED", lease_id
    return "OBSERVED", lease_id


def _verify_parameters(
    phase: str,
    run: dict[str, Any],
    receipt: dict[str, Any],
    expected_parameters: dict[str, Any] | None,
) -> bool | None:
    launch = _mapping(_value(receipt, "launch"))
    launch_extra = _mapping(_value(launch, "extra"))
    product_spec = _mapping(_value(launch_extra, "product_spec", "productSpec"))
    extra = _mapping(_value(product_spec, "extra"))
    if phase == "tiny":
        checkpoint = _mapping(_value(product_spec, "checkpoint"))
        return (
            _value(extra, "tiny_dry_run", "tinyDryRun") is True
            and _value(extra, "max_steps", "maxSteps") == 1
            and _value(extra, "max_train_samples", "maxTrainSamples") == 128
            and _value(checkpoint, "save_steps", "saveSteps") == 1
        )
    expected = {
        "epochs": 1,
        "perDeviceBatchSize": 1,
        "gradientAccumulationSteps": 1,
        "learningRate": 0.0002,
        "maxSequenceLength": 128,
        "maxSteps": 1,
        "loraRank": 8,
        "loraAlpha": 16,
        "loraDropout": 0.05,
        "template": "default",
        "modelRepository": "Qwen/Qwen2.5-1.5B-Instruct",
        "modelRevision": "5fee7c4ed634dc66c6e318c8ac2897b8b9154536",
        # Yield's currently constructed TrainingSpec uses CheckpointSpec's
        # source default. Transformers saves at max_steps even when this
        # interval is greater than the one-step run length.
        "checkpointSaveSteps": 100,
    }
    if expected_parameters:
        expected.update(expected_parameters)
    run_spec = _mapping(_value(run, "spec"))
    run_parameters = _mapping(_value(run_spec, "parameters"))
    hyperparams = _mapping(_value(product_spec, "hyperparams"))
    lora = _mapping(_value(product_spec, "lora"))
    llama_args = _mapping(_value(extra, "llamafactory_args", "llamafactoryArgs"))
    model = _mapping(_value(product_spec, "model"))
    quantization = _mapping(_value(product_spec, "quantization"))
    checkpoint = _mapping(_value(product_spec, "checkpoint"))
    base_source = _mapping(_value(extra, "base_source", "baseSource"))
    inputs = _value(extra, "input_artifacts", "inputArtifacts")
    input_by_kind = (
        {
            str(_value(_mapping(item), "kind")): _mapping(item)
            for item in inputs
            if isinstance(item, dict)
        }
        if isinstance(inputs, list)
        else {}
    )
    run_model = _mapping(_value(run_spec, "modelArtifact", "model_artifact"))
    run_dataset = _mapping(
        _value(_mapping(_value(run_spec, "datasetVersion", "dataset_version")), "artifact")
    )
    model_input = input_by_kind.get("model", {})
    dataset_input = input_by_kind.get("dataset", {})
    return (
        _value(run_spec, "method") == "SFT"
        and _value(run_spec, "finetuningType", "finetuning_type") == "LORA"
        and _value(run_parameters, "epochs") == expected["epochs"]
        and _value(run_parameters, "perDeviceBatchSize", "per_device_batch_size")
        == expected["perDeviceBatchSize"]
        and _value(run_parameters, "gradientAccumulationSteps", "gradient_accumulation_steps")
        == expected["gradientAccumulationSteps"]
        and _value(run_parameters, "learningRate", "learning_rate") == expected["learningRate"]
        and _value(run_parameters, "maxSequenceLength", "max_sequence_length")
        == expected["maxSequenceLength"]
        and _value(hyperparams, "num_train_epochs", "numTrainEpochs") == expected["epochs"]
        and _value(hyperparams, "per_device_batch_size", "perDeviceBatchSize")
        == expected["perDeviceBatchSize"]
        and _value(hyperparams, "gradient_accumulation_steps", "gradientAccumulationSteps")
        == expected["gradientAccumulationSteps"]
        and _value(hyperparams, "learning_rate", "learningRate") == expected["learningRate"]
        and _value(hyperparams, "max_seq_length", "maxSeqLength") == expected["maxSequenceLength"]
        and _value(extra, "max_steps", "maxSteps") == expected["maxSteps"]
        and _value(lora, "r") == expected["loraRank"]
        and _value(lora, "lora_alpha", "loraAlpha") == expected["loraAlpha"]
        and _value(lora, "lora_dropout", "loraDropout") == expected["loraDropout"]
        and _value(llama_args, "template") == expected["template"]
        and _value(llama_args, "bf16") is False
        and _value(llama_args, "fp16") is False
        and _value(model, "trust_remote_code", "trustRemoteCode") is False
        and _value(quantization, "use_4bit", "use4bit") is False
        and _value(checkpoint, "save_steps", "saveSteps") == expected["checkpointSaveSteps"]
        and _value(base_source, "repository") == expected["modelRepository"]
        and _value(base_source, "revision") == expected["modelRevision"]
        and _value(run_model, "digest") is not None
        and _value(run_model, "digest") == _value(model_input, "digest")
        and _value(run_dataset, "digest") is not None
        and _value(run_dataset, "digest") == _value(dataset_input, "digest")
    )


def _verify_artifact_manifest(
    run: dict[str, Any], path_value: str | Path
) -> tuple[str, dict[str, Any]]:
    try:
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            return "UNKNOWN", {}
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
        if not isinstance(document, dict) or set(document) != {"version", "files", "size_bytes"}:
            return "NOT_VERIFIED", {}
        if document["version"] != 2 or not isinstance(document["files"], list):
            return "NOT_VERIFIED", {}
        if type(document["size_bytes"]) is not int or document["size_bytes"] < 0:
            return "NOT_VERIFIED", {}
        files = document["files"]
        file_paths: list[str] = []
        total_size = 0
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {"path", "digest", "size_bytes"}:
                return "NOT_VERIFIED", {}
            relative = entry["path"]
            digest = entry["digest"]
            size = entry["size_bytes"]
            if (
                not isinstance(relative, str)
                or relative.startswith("/")
                or ".." in relative.split("/")
                or "\\" in relative
                or not isinstance(digest, str)
                or not _SHA256.fullmatch(digest)
                or type(size) is not int
                or size < 0
            ):
                return "NOT_VERIFIED", {}
            file_paths.append(relative)
            total_size += size
        canonical = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if (
            payload != canonical
            or file_paths != sorted(set(file_paths))
            or total_size != document["size_bytes"]
        ):
            return "NOT_VERIFIED", {}
        digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
        if not _SHA256.fullmatch(digest):
            return "NOT_VERIFIED", {}
        stem = path.name
        parent_names = [item.name for item in path.parents[:3]]
        if parent_names != [stem[:2], "sha256", "manifests"] or stem != digest.split(":", 1)[1]:
            return "NOT_VERIFIED", {}
        outputs = _value(run, "outputArtifacts", "output_artifacts")
        if not isinstance(outputs, list):
            return "UNKNOWN", {}
        matching_output = False
        for output in outputs:
            artifact = _mapping(_value(_mapping(output), "artifact"))
            if (
                _value(_mapping(output), "kind") == "MODEL_CHECKPOINT"
                and _value(artifact, "kind") == "checkpoint"
                and _value(artifact, "digest") == digest
                and _value(artifact, "manifestDigest", "manifest_digest") == digest
                and _value(artifact, "sizeBytes", "size_bytes") == document["size_bytes"]
            ):
                matching_output = True
                break
        result = _mapping(_value(run, "result"))
        result_checkpoints = _value(result, "checkpointArtifacts", "checkpoint_artifacts")
        if not isinstance(result_checkpoints, list):
            return "UNKNOWN", {}
        matching_result = any(
            _value(_mapping(item), "digest") == digest
            and _value(_mapping(item), "manifestDigest", "manifest_digest") == digest
            and _value(_mapping(item), "kind") == "checkpoint"
            and _value(_mapping(item), "sizeBytes", "size_bytes") == document["size_bytes"]
            for item in result_checkpoints
        )
        if not matching_output or not matching_result:
            return "NOT_VERIFIED", {}
        return "VERIFIED", {
            "artifact_manifest_digest": digest,
            "artifact_manifest_file_count": len(files),
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return "UNKNOWN", {}


def _verify_result_artifacts(run: dict[str, Any]) -> bool | None:
    result = _mapping(_value(run, "result"))
    adapter = _mapping(_value(result, "adapterArtifact", "adapter_artifact"))
    checkpoints = _value(result, "checkpointArtifacts", "checkpoint_artifacts")
    outputs = _value(run, "outputArtifacts", "output_artifacts")
    if (
        not result
        or not adapter
        or not isinstance(checkpoints, list)
        or not isinstance(outputs, list)
    ):
        return None
    adapter_digest = _value(adapter, "digest")
    output_adapters = {
        _value(_mapping(_value(_mapping(item), "artifact")), "digest")
        for item in outputs
        if _value(_mapping(item), "kind") == "MODEL_ADAPTER"
    }
    output_checkpoints = {
        _value(_mapping(_value(_mapping(item), "artifact")), "digest")
        for item in outputs
        if _value(_mapping(item), "kind") == "MODEL_CHECKPOINT"
    }
    checkpoint_digests = {
        _value(_mapping(item), "digest") for item in checkpoints if isinstance(item, dict)
    }
    if not _SHA256.fullmatch(adapter_digest or "") or not checkpoint_digests:
        return None
    if not all(_SHA256.fullmatch(item or "") for item in checkpoint_digests):
        return False
    return adapter_digest in output_adapters and checkpoint_digests <= output_checkpoints


def _optimizer_steps_from_manifest(path_value: str | Path) -> int | None:
    """Read the CAS trainer_state.json member, verifying its declared blob digest.

    从 CAS 目录清单读取 trainer_state.json，并校验成员 blob 的摘要。
    """
    try:
        manifest_path = Path(path_value)
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        document = json.loads(
            manifest_path.read_bytes().decode("utf-8"), object_pairs_hook=_reject_duplicates
        )
        if not isinstance(document, dict) or not isinstance(document.get("files"), list):
            return None
        trainer_entry = next(
            (
                item
                for item in document["files"]
                if isinstance(item, dict) and item.get("path", "").endswith("trainer_state.json")
            ),
            None,
        )
        if trainer_entry is None:
            return None
        digest = trainer_entry.get("digest")
        size = trainer_entry.get("size_bytes")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or type(size) is not int:
            return None
        parents = manifest_path.parents
        manifest_digest = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        manifest_hex = manifest_digest.split(":", 1)[1]
        if (
            len(parents) < 4
            or manifest_path.name != manifest_hex
            or [parents[index].name for index in range(3)]
            != [manifest_hex[:2], "sha256", "manifests"]
        ):
            return None
        artifact_root = parents[3]
        blob_digest = digest.split(":", 1)[1]
        blob_path = artifact_root / "blobs" / "sha256" / blob_digest[:2] / blob_digest
        if blob_path.is_symlink() or not blob_path.is_file():
            return None
        content = blob_path.read_bytes()
        if len(content) != size or "sha256:" + hashlib.sha256(content).hexdigest() != digest:
            return -1
        state = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicates)
        if not isinstance(state, dict) or type(state.get("global_step")) is not int:
            return None
        return state["global_step"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _blocked(report: dict[str, Any], status: str, reason: str) -> None:
    if not any(item["reason"] == reason for item in report["blockers"]):
        report["blockers"].append({"status": status, "reason": reason})


def _value(document: Any, *keys: str) -> Any:
    if not isinstance(document, dict):
        return None
    for key in keys:
        if key in document:
            return document[key]
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _identifier(document: Any, *keys: str) -> str | None:
    value = _value(document, *keys)
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None
