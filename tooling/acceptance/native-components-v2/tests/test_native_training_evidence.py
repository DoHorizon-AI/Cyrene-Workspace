"""Synthetic contract tests for the offline evidence checker; no host evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import uuid
from pathlib import Path
from typing import Any

_MODULE_PATH = Path(__file__).parents[1] / "training_evidence.py"
_SPEC = importlib.util.spec_from_file_location("training_evidence", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
verify_training_attempt = _MODULE.verify_training_attempt
verify_training_pair = _MODULE.verify_training_pair


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _artifact(tmp_path: Path) -> tuple[Path, str, int]:
    root = tmp_path / "artifact-root"
    trainer_state = _canonical({"global_step": 1, "log_history": [{"step": 1}]})
    trainer_digest = _digest(trainer_state)
    blob = root / "blobs" / "sha256" / trainer_digest[7:9] / trainer_digest[7:]
    blob.parent.mkdir(parents=True)
    blob.write_bytes(trainer_state)
    manifest = {
        "version": 2,
        "files": [
            {
                "path": "trainer_state.json",
                "digest": trainer_digest,
                "size_bytes": len(trainer_state),
            }
        ],
        "size_bytes": len(trainer_state),
    }
    manifest_bytes = _canonical(manifest)
    manifest_digest = _digest(manifest_bytes)
    path = root / "manifests" / "sha256" / manifest_digest[7:9] / manifest_digest[7:]
    path.parent.mkdir(parents=True)
    path.write_bytes(manifest_bytes)
    return path, manifest_digest, len(trainer_state)


def _evidence(tmp_path: Path, *, phase: str, suffix: str = "a") -> dict[str, Any]:
    run_id = "11111111-1111-4111-8111-111111111111"
    draft_id = "22222222-2222-4222-8222-222222222222"
    raw_attempt_id = f"phase-{phase}-{suffix}"
    attempt_uri = f"cyrene://yield/training-runs/{run_id}/attempts/{raw_attempt_id}"
    public_attempt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, attempt_uri))
    spec: dict[str, Any] = {
        "engine": "llama_factory",
        "job_id": raw_attempt_id,
        "finetuning_type": "lora",
        "stage": "sft",
        "model": {"trust_remote_code": False},
        "quantization": {"use_4bit": False},
        "checkpoint": {
            "save_steps": 1 if phase == "tiny" else 100,
            "save_total_limit": 1 if phase == "tiny" else 3,
        },
        "hyperparams": {
            "num_train_epochs": 1,
            "per_device_batch_size": 1,
            "gradient_accumulation_steps": 1,
            "learning_rate": 0.0002,
            "max_seq_length": 128,
        },
        "lora": {"r": 8, "lora_alpha": 16, "lora_dropout": 0.05},
        "extra": {
            "max_steps": 1,
            "tiny_dry_run": phase == "tiny",
            "max_train_samples": 128 if phase == "tiny" else None,
            "base_source": {
                "repository": "Qwen/Qwen2.5-1.5B-Instruct",
                "revision": "5fee7c4ed634dc66c6e318c8ac2897b8b9154536",
            },
            "input_artifacts": [
                {"digest": "sha256:" + "2" * 64, "kind": "dataset"},
                {"digest": "sha256:" + "1" * 64, "kind": "model"},
            ],
            "llamafactory_args": {"template": "default", "bf16": False, "fp16": False},
        },
    }
    manifest_path, manifest_digest, artifact_size = _artifact(tmp_path / phase)
    run = {
        "id": run_id,
        "draftId": draft_id,
        "state": "COMPLETED",
        "result": {
            "id": "33333333-3333-4333-8333-333333333333",
            "adapterArtifact": {"digest": "sha256:" + "3" * 64, "kind": "model"},
            "checkpointArtifacts": [
                {
                    "digest": manifest_digest,
                    "manifestDigest": manifest_digest,
                    "kind": "checkpoint",
                    "sizeBytes": artifact_size,
                }
            ],
        },
        "spec": {
            "method": "SFT",
            "finetuningType": "LORA",
            "modelArtifact": {"digest": "sha256:" + "1" * 64, "kind": "model"},
            "datasetVersion": {"artifact": {"digest": "sha256:" + "2" * 64, "kind": "dataset"}},
            "parameters": {
                "epochs": 1,
                "perDeviceBatchSize": 1,
                "gradientAccumulationSteps": 1,
                "learningRate": 0.0002,
                "maxSequenceLength": 128,
            },
        },
        "outputArtifacts": [
            {
                "kind": "MODEL_ADAPTER",
                "artifact": {"digest": "sha256:" + "3" * 64, "kind": "model"},
                "derivedFromDigests": [],
            },
            {
                "kind": "MODEL_CHECKPOINT",
                "artifact": {
                    "digest": manifest_digest,
                    "manifestDigest": manifest_digest,
                    "kind": "checkpoint",
                    "sizeBytes": artifact_size,
                },
                "derivedFromDigests": [],
            },
        ],
    }
    worker = {"id": f"yield-worker-{suffix}", "generation": 1}
    lease_identity = {"id": f"lease-{suffix}", "generation": 1}
    resource = {"id": f"gpu-{suffix}", "generation": 1}
    release = {
        "identity": lease_identity,
        "holder": worker,
        "resources": [resource],
        "state": "LEASE_STATE_RELEASED",
        "fenceToken": 29,
    }
    receipt = {
        "worker": worker,
        "node": {"nodeId": "node-fixture"},
        "launch": {
            "extra": {"product_spec": spec, "attempt_id": raw_attempt_id + ":1"},
            "env": {"FIXTURE_SECRET": "must-not-appear"},
        },
        "lease": {
            "identity": lease_identity,
            "holder": worker,
            "resources": [resource],
            "fenceToken": 29,
        },
        "operation": {
            "identity": {"id": f"operation/start/{worker['id']}", "generation": 1},
            "metadata": {"worker.id": worker["id"]},
        },
        "released": True,
        "releaseReceipt": release,
    }
    phase_id = "training-tiny-dry-run" if phase == "tiny" else "training-execution"
    attempts = [
        {"id": public_attempt_id, "trainingRunId": run_id, "phase": phase_id, "state": "SUCCEEDED"}
    ]
    events = [
        {
            "trainingRunId": run_id,
            "attemptId": raw_attempt_id,
            "phase": phase_id,
            "kind": "log",
            "step": 1,
        }
    ]
    readiness = [
        {
            "activeTaskCount": 1,
            "activeLeaseOrAllocationCount": 1,
            "activeTasks": [{"sourceId": "cyrene-yield", "taskId": draft_id, "state": "RUNNING"}],
        }
    ]
    return {
        "run": run,
        "attempts": attempts,
        "events": events,
        "execution_receipt": receipt,
        "kernel_readiness": readiness,
        "release_response": release,
        "artifact_manifest_path": manifest_path,
    }


def test_successful_synthetic_attempt_keeps_kernel_release_unknown(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, phase="formal")
    report = verify_training_attempt(phase="formal", **evidence)

    assert report["training_status"] == "VERIFIED"
    assert report["owner_release_status"] == "OBSERVED"
    assert report["kernel_resource_release_status"] == "UNKNOWN"
    assert report["verified"]["completed_optimizer_steps"] == 1
    assert report["verified"]["lease_id"] == "lease-a"
    assert "fenceToken" not in json.dumps(report)
    assert "FIXTURE_SECRET" not in json.dumps(report)


def test_cross_task_readiness_cannot_link_the_run(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, phase="formal")
    evidence["kernel_readiness"][0]["activeTasks"][0]["taskId"] = "another-draft"

    report = verify_training_attempt(phase="formal", **evidence)

    assert report["training_status"] == "UNKNOWN"
    assert {item["reason"] for item in report["blockers"]} >= {"matching_kernel_task_not_observed"}


def test_missing_optimizer_artifact_stays_unknown_even_when_event_reports_step_one(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path, phase="formal")
    manifest_path = evidence["artifact_manifest_path"]
    document = json.loads(manifest_path.read_text())
    document["files"] = []
    document["size_bytes"] = 0
    payload = _canonical(document)
    digest = _digest(payload)
    new_path = manifest_path.parents[2] / "sha256" / digest[7:9] / digest[7:]
    new_path.parent.mkdir(parents=True)
    manifest_path.unlink()
    new_path.write_bytes(payload)
    evidence["artifact_manifest_path"] = new_path
    artifact = evidence["run"]["outputArtifacts"][1]["artifact"]
    artifact["digest"] = digest
    artifact["manifestDigest"] = digest
    artifact["sizeBytes"] = 0
    evidence["run"]["result"]["checkpointArtifacts"][0]["digest"] = digest
    evidence["run"]["result"]["checkpointArtifacts"][0]["manifestDigest"] = digest
    evidence["run"]["result"]["checkpointArtifacts"][0]["sizeBytes"] = 0

    report = verify_training_attempt(phase="formal", **evidence)

    assert report["training_status"] == "UNKNOWN"
    assert "trainer_state_global_step_missing" in {item["reason"] for item in report["blockers"]}
    assert report["verified"].get("completed_optimizer_steps") is None


def test_failed_kernel_release_is_not_owner_release_success(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, phase="formal")
    evidence["release_response"] = {**evidence["release_response"], "state": "LEASE_STATE_FAILED"}

    report = verify_training_attempt(phase="formal", **evidence)

    assert report["owner_release_status"] == "NOT_VERIFIED"
    assert report["kernel_resource_release_status"] == "UNKNOWN"
    assert report["training_status"] == "NOT_VERIFIED"


def test_manifest_digest_mismatch_is_not_verified(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, phase="formal")
    evidence["run"]["outputArtifacts"][1]["artifact"]["digest"] = "sha256:" + "0" * 64

    report = verify_training_attempt(phase="formal", **evidence)

    assert report["training_status"] == "NOT_VERIFIED"
    assert "artifact_manifest_mismatch" in {item["reason"] for item in report["blockers"]}


def test_tiny_and_formal_may_not_reuse_attempt_worker_or_lease(tmp_path: Path) -> None:
    tiny = _evidence(tmp_path, phase="tiny", suffix="same")
    formal = _evidence(tmp_path, phase="formal", suffix="same")
    formal["execution_receipt"]["launch"]["extra"]["product_spec"]["job_id"] = tiny[
        "execution_receipt"
    ]["launch"]["extra"]["product_spec"]["job_id"]
    formal["execution_receipt"]["launch"]["extra"]["attempt_id"] = tiny["execution_receipt"][
        "launch"
    ]["extra"]["attempt_id"]
    formal["execution_receipt"]["worker"] = tiny["execution_receipt"]["worker"]
    formal["execution_receipt"]["lease"] = tiny["execution_receipt"]["lease"]

    report = verify_training_pair(tiny=tiny, formal=formal)

    assert report["training_status"] == "NOT_VERIFIED"
    assert report["tiny"]["training_status"] == "NOT_VERIFIED"
    assert report["formal"]["training_status"] == "NOT_VERIFIED"


def test_distinct_tiny_and_formal_attempts_are_recorded_separately(tmp_path: Path) -> None:
    tiny = _evidence(tmp_path, phase="tiny", suffix="tiny")
    formal = _evidence(tmp_path, phase="formal", suffix="formal")

    report = verify_training_pair(tiny=tiny, formal=formal)

    assert report["tiny"]["phase"] == "tiny"
    assert report["formal"]["phase"] == "formal"
    assert report["tiny"]["training_status"] == "VERIFIED"
    assert report["formal"]["training_status"] == "VERIFIED"
    assert report["training_status"] == "VERIFIED"
    assert report["acceptance_status"] == "NOT_VERIFIED"
    assert report["kernel_resource_release_status"] == "UNKNOWN"


def test_non_checkpoint_artifact_with_step_state_is_rejected(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, phase="formal")
    evidence["run"]["outputArtifacts"][1]["kind"] = "METRICS"
    evidence["run"]["outputArtifacts"][1]["artifact"]["kind"] = "metrics"

    report = verify_training_attempt(phase="formal", **evidence)

    assert report["training_status"] == "NOT_VERIFIED"
    assert "artifact_manifest_mismatch" in {item["reason"] for item in report["blockers"]}
    assert report["verified"].get("completed_optimizer_steps") is None


def test_missing_result_checkpoint_association_is_rejected(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path, phase="formal")
    evidence["run"]["result"]["checkpointArtifacts"] = []

    report = verify_training_attempt(phase="formal", **evidence)

    assert report["training_status"] == "NOT_VERIFIED"
    assert "artifact_manifest_mismatch" in {item["reason"] for item in report["blockers"]}
    assert report["verified"].get("completed_optimizer_steps") is None


def test_formal_default_checkpoint_interval_accepts_checkpoint_with_step_one(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path, phase="formal")

    report = verify_training_attempt(phase="formal", **evidence)

    assert report["training_status"] == "VERIFIED"
    assert report["verified"]["completed_optimizer_steps"] == 1
