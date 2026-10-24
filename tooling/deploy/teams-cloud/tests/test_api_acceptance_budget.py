"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_api_acceptance_budget.py                                        │
│  Module: tooling.deploy.teams_cloud.tests.test_api_acceptance_budget    │
│  Role: Prove API reservation, replay, cleanup, and evidence gates.       │
│                                                                          │
│  模块职责：验证 API 预算预留、防重放、清理与证据门槛。                  │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
budget = importlib.import_module("api_acceptance_budget")


OWNER = "11111111-1111-4111-8111-111111111111"
TASK_ID = "teams-cloud-20261023"
START = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _write_private(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _github_hf_receipt(path: Path) -> bytes:
    providers = {
        provider: {
            "identityStatus": "VERIFIED",
            "privateReadStatus": "VERIFIED",
            "writeReadbackStatus": "VERIFIED",
            "cleanupReadbackStatus": "ABSENT",
            "resourceRef": f"{provider}-private-test-resource",
            "writeReadbackSha256": "a" * 64,
            "cleanupReadbackAt": "2026-10-23T11:30:00Z",
        }
        for provider in ("github", "huggingface")
    }
    document = {
        "schemaVersion": "cyrene-cloud-acceptance-evidence-v1",
        "stage": "github_hf",
        "status": "VERIFIED",
        "evidenceClass": "REAL_PROVIDER_API",
        "verifiedAt": "2026-10-23T11:30:00Z",
        "taskId": TASK_ID,
        "owner": OWNER,
        "reservedUsd": 0.0,
        "cumulativeReservedUsd": 0.0,
        "providers": providers,
    }
    _write_private(path, document)
    return path.read_bytes()


def _new_azure_gcp_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[budget.ApiAcceptanceLedger, Path]:
    artifacts = tmp_path / ".artifacts" / TASK_ID
    evidence_dir = artifacts / "acceptance"
    evidence_dir.mkdir(parents=True, mode=0o700)
    evidence_dir.chmod(0o700)
    previous = evidence_dir / "github-hf.json"
    _github_hf_receipt(previous)
    clock = {"now": START}
    monkeypatch.setattr(budget, "_now_utc", lambda _value=None: clock["now"])
    instance = budget.ApiAcceptanceLedger(
        evidence_dir / "azure-gcp.api-reservations.json",
        task_id=TASK_ID,
        owner=OWNER,
        stage="azure_gcp",
        previous_evidence=previous,
        now=START,
    )
    return instance, evidence_dir


def _reserve(
    ledger: budget.ApiAcceptanceLedger, resource_id: str, provider: str, amount: float
) -> None:
    ledger.reserve_resource(
        resource_id=resource_id,
        provider=provider,
        marker=f"cyrene-{TASK_ID}-{provider}-{resource_id}",
        profile_scope_digest_sha256="b" * 64,
        target_digest_sha256="c" * 64,
        target_ref=f"{TASK_ID}-{provider}-{resource_id}",
        quote_ref=f"https://pricing.example.net/{provider}/sku",
        quoted_at="2026-10-23T11:55:00Z",
        upper_bound_usd=amount,
    )


def _exercise_and_cleanup(
    ledger: budget.ApiAcceptanceLedger,
    resource_id: str,
    provider: str,
    clock: dict[str, datetime],
) -> None:
    base = clock["now"]

    def iso(offset: int) -> str:
        return (base + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")

    create_id = ledger.begin_mutation(
        resource_id,
        tool_id="cloud_resource_create",
        operation_id="cloud.resource.create",
        request_digest_sha256="d" * 64,
        resource_ref=f"{provider}-profile/create/resource",
        timeout_seconds=300,
    )
    ledger.record_dispatch(
        resource_id,
        create_id,
        grant_ref=f"grant-{resource_id}-create",
        work_operation_id=f"work-{resource_id}-create",
        outcome="ACCEPTED",
        operation_ref=f"operation-{resource_id}-create",
    )
    clock["now"] = base + timedelta(seconds=10)
    ledger.record_readback(
        resource_id,
        create_id,
        outcome="SUCCEEDED",
        state_digest_sha256="e" * 64,
        result_digest_sha256="f" * 64,
        observed_at=iso(10),
        vendor_ref=f"{provider}-vendor-resource-{resource_id}",
        actual_state="RUNNING",
        readiness="READY",
    )

    clock["now"] = base + timedelta(seconds=599)
    delete_id = ledger.begin_mutation(
        resource_id,
        tool_id="cloud_resource_delete",
        operation_id="cloud.resource.delete",
        request_digest_sha256="1" * 64,
        resource_ref=f"{provider}-profile/delete/resource",
        timeout_seconds=300,
    )
    ledger.record_dispatch(
        resource_id,
        delete_id,
        grant_ref=f"grant-{resource_id}-delete",
        work_operation_id=f"work-{resource_id}-delete",
        outcome="ACCEPTED",
        operation_ref=f"operation-{resource_id}-delete",
    )
    clock["now"] = base + timedelta(seconds=700)
    ledger.record_readback(
        resource_id,
        delete_id,
        outcome="SUCCEEDED",
        state_digest_sha256="2" * 64,
        result_digest_sha256="3" * 64,
        observed_at=iso(700),
        actual_state="ABSENT",
        readiness="NOT_READY",
    )
    with pytest.raises(
        budget.ApiBudgetError, match="must follow cleanup dispatch and delete read-back"
    ):
        ledger.mark_cleanup_absent(
            resource_id,
            observed_at=iso(699),
            state_digest_sha256="4" * 64,
        )
    ledger.mark_cleanup_absent(
        resource_id,
        observed_at=iso(710),
        state_digest_sha256="4" * 64,
    )


def test_api_budget_reserves_before_dispatch_and_finalizes_only_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Build a synthetic ledger only; no DSH or vendor request is dispatched by the library."""

    ledger, evidence_dir = _new_azure_gcp_ledger(tmp_path, monkeypatch)
    clock = {"now": START}
    monkeypatch.setattr(budget, "_now_utc", lambda _value=None: clock["now"])
    _reserve(ledger, "azure-vnet-test", "azure", 0.75)
    _reserve(ledger, "gcp-vm-test", "gcp", 0.25)
    _exercise_and_cleanup(ledger, "azure-vnet-test", "azure", clock)
    _exercise_and_cleanup(ledger, "gcp-vm-test", "gcp", clock)

    receipt_path = evidence_dir / "provider-plans" / "azure-gcp-api-evidence.json"
    receipt_path.parent.mkdir(mode=0o700)
    receipt_path.parent.chmod(0o700)
    receipt = ledger.finalize(receipt_path)
    assert receipt["status"] == "VERIFIED"
    assert receipt["evidenceClass"] == "REAL_PROVIDER_API"
    assert receipt["executionPath"] == "navigator_dsh_exchange_directplugin_api"
    assert receipt["reservedUsd"] == 1.0
    assert receipt["cumulativeReservedUsd"] == 1.0
    assert {row["providerId"] for row in receipt["providers"]} == {"azure", "gcp"}
    assert all(
        resource["cleanupAbsenceStatus"] == "ABSENT"
        for provider in receipt["providers"]
        for resource in provider["resources"]
    )
    assert (
        receipt["previousEvidenceSha256"]
        == hashlib.sha256((evidence_dir / "github-hf.json").read_bytes()).hexdigest()
    )
    assert receipt_path.stat().st_mode & 0o077 == 0


def test_unknown_create_is_durable_and_cannot_be_replayed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An uncertain create requires typed read-back before the caller can make another write."""

    ledger, _evidence_dir = _new_azure_gcp_ledger(tmp_path, monkeypatch)
    _reserve(ledger, "azure-resource-test", "azure", 1.0)
    first = ledger.begin_mutation(
        "azure-resource-test",
        tool_id="cloud_resource_create",
        operation_id="cloud.resource.create",
        request_digest_sha256="d" * 64,
        resource_ref="azure-profile/resource/create-target",
    )
    state = ledger.snapshot()["resourceStates"]["azure-resource-test"]
    assert state["unresolvedWrites"] == 1
    ledger.record_dispatch(
        "azure-resource-test",
        first,
        grant_ref="",
        work_operation_id="",
        outcome="UNKNOWN",
    )
    with pytest.raises(budget.ApiBudgetError, match="after the write dispatch"):
        ledger.record_readback(
            "azure-resource-test",
            first,
            outcome="UNKNOWN",
            state_digest_sha256="",
            result_digest_sha256="",
            observed_at="2026-10-23T11:59:59Z",
            actual_state="UNKNOWN",
            readiness="UNKNOWN",
        )
    with pytest.raises(budget.ApiBudgetError, match="reconcile the prior write"):
        ledger.begin_mutation(
            "azure-resource-test",
            tool_id="cloud_resource_create",
            operation_id="cloud.resource.create",
            request_digest_sha256="d" * 64,
            resource_ref="azure-profile/resource/create-target",
        )
    state = ledger.snapshot()["resourceStates"]["azure-resource-test"]
    assert state["unresolvedWrites"] == 1


def test_late_dispatch_becomes_unknown_and_breaches_the_reserved_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dispatch response after its reserved timeout cannot become success evidence."""

    ledger, _evidence_dir = _new_azure_gcp_ledger(tmp_path, monkeypatch)
    clock = {"now": START}
    monkeypatch.setattr(budget, "_now_utc", lambda _value=None: clock["now"])
    _reserve(ledger, "azure-resource-test", "azure", 1.0)
    write_id = ledger.begin_mutation(
        "azure-resource-test",
        tool_id="cloud_resource_create",
        operation_id="cloud.resource.create",
        request_digest_sha256="d" * 64,
        resource_ref="azure-profile/resource/create-target",
        timeout_seconds=300,
    )
    clock["now"] = START + timedelta(seconds=301)
    ledger.record_dispatch(
        "azure-resource-test",
        write_id,
        grant_ref="grant-create",
        work_operation_id="work-create",
        outcome="ACCEPTED",
        operation_ref="operation-create",
    )
    state = ledger.snapshot()["resourceStates"]["azure-resource-test"]
    assert state["unresolvedWrites"] == 1
    assert state["windowBreached"] is True


def test_cleanup_must_begin_before_the_reserved_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The final 300 seconds are reserved for deletion and absence read-back."""

    ledger, _evidence_dir = _new_azure_gcp_ledger(tmp_path, monkeypatch)
    clock = {"now": START}
    monkeypatch.setattr(budget, "_now_utc", lambda _value=None: clock["now"])
    _reserve(ledger, "azure-resource-test", "azure", 1.0)
    create_id = ledger.begin_mutation(
        "azure-resource-test",
        tool_id="cloud_resource_create",
        operation_id="cloud.resource.create",
        request_digest_sha256="d" * 64,
        resource_ref="azure-profile/resource/create-target",
    )
    ledger.record_dispatch(
        "azure-resource-test",
        create_id,
        grant_ref="grant-create",
        work_operation_id="work-create",
        outcome="ACCEPTED",
        operation_ref="operation-create",
    )
    clock["now"] = START + timedelta(seconds=10)
    ledger.record_readback(
        "azure-resource-test",
        create_id,
        outcome="SUCCEEDED",
        state_digest_sha256="e" * 64,
        result_digest_sha256="f" * 64,
        observed_at="2026-10-23T12:00:10Z",
        vendor_ref="azure-vendor-resource",
        actual_state="RUNNING",
        readiness="READY",
    )
    clock["now"] = START + timedelta(seconds=600)
    with pytest.raises(budget.ApiBudgetError, match="cleanup reserve has started"):
        ledger.begin_mutation(
            "azure-resource-test",
            tool_id="cloud_resource_delete",
            operation_id="cloud.resource.delete",
            request_digest_sha256="1" * 64,
            resource_ref="azure-profile/resource/delete-target",
            timeout_seconds=300,
        )


def test_runpod_budget_chains_to_exact_azure_gcp_api_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RunPod carries forward the prior typed API reservation and raw receipt digest."""

    azure_ledger, evidence_dir = _new_azure_gcp_ledger(tmp_path, monkeypatch)
    clock = {"now": START}
    monkeypatch.setattr(budget, "_now_utc", lambda _value=None: clock["now"])
    _reserve(azure_ledger, "azure-test", "azure", 0.75)
    _reserve(azure_ledger, "gcp-test", "gcp", 0.25)
    _exercise_and_cleanup(azure_ledger, "azure-test", "azure", clock)
    _exercise_and_cleanup(azure_ledger, "gcp-test", "gcp", clock)
    stage2 = evidence_dir / "azure-gcp-api-evidence.json"
    stage2.parent.mkdir(mode=0o700, exist_ok=True)
    stage2.parent.chmod(0o700)
    azure_ledger.finalize(stage2)

    runpod = budget.ApiAcceptanceLedger(
        evidence_dir / "runpod.api-reservations.json",
        task_id=TASK_ID,
        owner=OWNER,
        stage="runpod",
        previous_evidence=stage2,
        github_hf_evidence=evidence_dir / "github-hf.json",
        now=START,
    )
    _reserve(runpod, "runpod-pod-test", "runpod", 0.5)
    snapshot = runpod.snapshot()
    assert snapshot["reservedUsd"] == 0.5
    assert snapshot["cumulativeReservedUsd"] == 1.5
