"""Focused tests for digest-bound held first-Core successor plans.

These tests cover the immutable lineage contract; host recovery remains a separate
integration check because no live Broker is contacted here.
中文：验证后继计划绑定，不连接实际主机或 Broker。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "native_core_successor_test", ROOT / "packaging" / "native_core_successor.py"
)
assert _SPEC is not None and _SPEC.loader is not None
successor = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = successor
_SPEC.loader.exec_module(successor)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def plan_inputs() -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    component_ids = (
        "cyrene-linux-sys-adapter",
        "cyrene-nvidia-adapter",
        "cyrene-sandboxd",
        "cyrene-kernel",
    )
    core = SimpleNamespace(CORE_COMPONENT_IDS=component_ids, CORE_BOOTSTRAP_MODE="first-core")
    parent_artifacts = {
        component_id: _digest((component_id + "-old").encode()) for component_id in component_ids
    }
    parent = {
        "planId": "plan-" + "a" * 32,
        "planDigest": _digest(b"parent-plan"),
        "requestId": "bootstrap-parent",
        "componentArtifactDigests": parent_artifacts,
        "includeProducts": False,
        "gateGeneration": 0,
        "catalogGeneration": 1,
        "catalogDigest": _digest(b"activity-catalog"),
        "activitySources": ["source-a", "source-b"],
        "targetId": "ubuntu-22.04-x86_64",
    }
    transaction = {
        "phase": "hold_required",
        "requestId": parent["requestId"],
        "maintenanceGateGeneration": 1,
        "beginRequest": {"request_id": parent["requestId"], "target_kind": "CORE_RUNTIME"},
    }
    items = [
        {
            "componentId": component_id,
            "version": "2.0.0",
            "manifestDigest": _digest((component_id + "-manifest").encode()),
            "artifactDigest": _digest((component_id + "-new").encode()),
            "sourceCommit": "b" * 40,
            "restartGroup": "core-runtime",
        }
        for component_id in component_ids
    ]
    normal = {
        "planId": "plan-" + "b" * 32,
        "planDigest": _digest(b"signed-candidate-plan"),
        "channel": "preview",
        "catalogGeneration": 9,
        "catalogDigest": _digest(b"new signed component catalog"),
        "components": items,
    }
    snapshot = {
        "gateGeneration": 1,
        "catalogGeneration": 1,
        "activitySources": ["source-a", "source-b"],
        "coreBootstrapEligible": False,
    }
    return core, parent, transaction, normal, snapshot


def test_successor_plan_binds_parent_authority_and_actual_held_generation(
    plan_inputs: tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]],
) -> None:
    core, parent, transaction, normal, snapshot = plan_inputs

    plan = successor.build_plan(
        core, parent, transaction, normal, snapshot, "ubuntu-22.04-x86_64", _canonical
    )

    binding = plan["successorBinding"]
    assert plan["gateGeneration"] == 0
    assert plan["coreBootstrapEligible"] is False
    assert binding["maintenanceGateGeneration"] == 1
    assert binding["parentPlanId"] == parent["planId"]
    assert binding["parentPlanDigest"] == parent["planDigest"]
    assert binding["parentComponentArtifactDigests"] == parent["componentArtifactDigests"]
    assert plan["componentArtifactDigests"] != parent["componentArtifactDigests"]
    assert {item["sourceCommit"] for item in plan["components"]} == {"b" * 40}
    assert binding["parentBeginRequestDigest"] == _digest(_canonical(transaction["beginRequest"]))
    assert binding["candidateCatalogGeneration"] == normal["catalogGeneration"]
    assert binding["candidateCatalogDigest"] == normal["catalogDigest"]
    assert plan["catalogGeneration"] == snapshot["catalogGeneration"]
    assert plan["catalogDigest"] == normal["catalogDigest"]
    assert successor.selector_for_plan(plan) == {
        "schemaVersion": 1,
        "mode": "held-successor",
        "parentPlanId": parent["planId"],
        "parentPlanDigest": parent["planDigest"],
        "parentComponentArtifactDigests": parent["componentArtifactDigests"],
        "maintenanceGateGeneration": 1,
    }
    assert _digest(_canonical(successor.plan_material(plan))) == plan["planDigest"]
    assert plan["planId"] == "plan-" + plan["planDigest"].split(":", 1)[1][:32]


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "schemaVersion": 1,
            "mode": "held-successor",
            "parentPlanId": "plan-" + "a" * 32,
            "parentPlanDigest": _digest(b"parent"),
            "parentComponentArtifactDigests": {"core": _digest(b"x")},
            "maintenanceGateGeneration": True,
        },
        {
            "schemaVersion": 1,
            "mode": "held-successor",
            "parentPlanId": "plan-" + "a" * 32,
            "parentPlanDigest": _digest(b"parent"),
            "parentComponentArtifactDigests": {"core": "sha256:bad"},
            "maintenanceGateGeneration": 1,
        },
    ],
)
def test_selector_rejects_boolean_generation_and_malformed_artifact_digest(
    mutation: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        successor.validate_selector(mutation)


def test_successor_plan_allows_new_trusted_catalog_but_rejects_profile_or_product_changes(
    plan_inputs: tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]],
) -> None:
    core, parent, transaction, normal, snapshot = plan_inputs
    changed_catalog = {
        **normal,
        "catalogGeneration": 10,
        "catalogDigest": _digest(b"newer catalog"),
    }
    plan = successor.build_plan(
        core, parent, transaction, changed_catalog, snapshot, "ubuntu-22.04-x86_64", _canonical
    )
    assert plan["successorBinding"]["candidateCatalogGeneration"] == 10
    with pytest.raises(ValueError, match="supported target"):
        successor.build_plan(
            core, parent, transaction, changed_catalog, snapshot, "ubuntu-24.04-x86_64", _canonical
        )
    product_parent = {**parent, "includeProducts": True}
    with pytest.raises(ValueError, match="Core cohort"):
        successor.build_plan(
            core, product_parent, transaction, normal, snapshot, "ubuntu-22.04-x86_64", _canonical
        )
    unknown_component = {
        **normal,
        "components": [
            *normal["components"][:-1],
            {**normal["components"][-1], "componentId": "cyrene-unknown"},
        ],
    }
    with pytest.raises(ValueError, match="four-component Core cohort"):
        successor.build_plan(
            core,
            parent,
            transaction,
            unknown_component,
            snapshot,
            "ubuntu-22.04-x86_64",
            _canonical,
        )


def test_candidate_validation_failure_precedes_begin_replay(
    monkeypatch: pytest.MonkeyPatch,
    plan_inputs: tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]],
) -> None:
    core, parent, transaction, _normal, _snapshot = plan_inputs
    calls: list[str] = []

    class CandidateUpdater:
        def _require_authorized_process(self) -> None:
            return None

        def _reload_catalog_for_operation(self) -> None:
            return None

        def _ensure_state_root(self) -> None:
            return None

        def _exclusive_update_lock(self) -> Any:
            return nullcontext()

        def _resolve_channel(self, channel: str) -> str:
            return channel

        def check(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise ValueError("signature verification failed")

    selector = {
        "schemaVersion": 1,
        "mode": "held-successor",
        "parentPlanId": parent["planId"],
        "parentPlanDigest": parent["planDigest"],
        "parentComponentArtifactDigests": parent["componentArtifactDigests"],
        "maintenanceGateGeneration": 1,
    }
    monkeypatch.setattr(successor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        successor, "_parent_context", lambda *_args: (selector, parent, transaction)
    )
    monkeypatch.setattr(successor, "_existing_attempt", lambda *_args: None)
    monkeypatch.setattr(
        successor,
        "_confirm_parent_hold",
        lambda *_args: calls.append("replayed-original-begin"),
    )
    with pytest.raises(ValueError, match="signature verification failed"):
        successor.check(CandidateUpdater(), selector, channel="preview", core=core)
    assert calls == []
