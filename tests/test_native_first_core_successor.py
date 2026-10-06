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
_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "native_core_bootstrap_successor_fixtures", ROOT / "tests" / "test_native_core_bootstrap.py"
)
assert _FIXTURE_SPEC is not None and _FIXTURE_SPEC.loader is not None
fixtures = importlib.util.module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = fixtures
_FIXTURE_SPEC.loader.exec_module(fixtures)
core_runtime = fixtures.bootstrap
fixtures.canonical_jcs = fixtures.updates.canonical_jcs


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _private_state_dir(updater: Any, name: str) -> Path:
    path = Path(updater.state_root) / name
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


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
    parent_components = [
        {
            "componentId": component_id,
            "version": "1.0.0",
            "manifestDigest": _digest((component_id + "-old-manifest").encode()),
            "artifactDigest": parent_artifacts[component_id],
            "sourceCommit": "a" * 40,
            "restartGroup": "core-runtime",
        }
        for component_id in component_ids
    ]
    parent = {
        "planId": "plan-" + "a" * 32,
        "planDigest": _digest(b"parent-plan"),
        "requestId": "bootstrap-parent",
        "componentArtifactDigests": parent_artifacts,
        "components": parent_components,
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
            "version": "2.0.0" if component_id == "cyrene-sandboxd" else "1.0.0",
            "manifestDigest": (
                _digest((component_id + "-new-manifest").encode())
                if component_id == "cyrene-sandboxd"
                else _digest((component_id + "-old-manifest").encode())
            ),
            "artifactDigest": (
                _digest((component_id + "-new").encode())
                if component_id == "cyrene-sandboxd"
                else parent_artifacts[component_id]
            ),
            "sourceCommit": "b" * 40 if component_id == "cyrene-sandboxd" else "a" * 40,
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
        "replacementReleaseId": "preview-sandboxd-fixed-release",
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
    assert (
        next(
            item["sourceCommit"]
            for item in plan["components"]
            if item["componentId"] == "cyrene-sandboxd"
        )
        == "b" * 40
    )
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
        "replacementReleaseId": normal["replacementReleaseId"],
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
            "replacementReleaseId": "preview-sandboxd-fixed-release",
        },
        {
            "schemaVersion": 1,
            "mode": "held-successor",
            "parentPlanId": "plan-" + "a" * 32,
            "parentPlanDigest": _digest(b"parent"),
            "parentComponentArtifactDigests": {"core": "sha256:bad"},
            "maintenanceGateGeneration": 1,
            "replacementReleaseId": "preview-sandboxd-fixed-release",
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
    changed_kernel = {
        **changed_catalog,
        "components": [
            {**item, "artifactDigest": _digest(b"unexpected Kernel change")}
            if item["componentId"] == "cyrene-kernel"
            else item
            for item in changed_catalog["components"]
        ],
    }
    with pytest.raises(ValueError, match="only cyrene-sandboxd"):
        successor.build_plan(
            core, parent, transaction, changed_kernel, snapshot, "ubuntu-22.04-x86_64", _canonical
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

        def _catalog_core_component_ids(self) -> tuple[str, ...]:
            return tuple(core.CORE_COMPONENT_IDS)

        def check(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise ValueError("signature verification failed")

    selector = {
        "schemaVersion": 1,
        "mode": "held-successor",
        "parentPlanId": parent["planId"],
        "parentPlanDigest": parent["planDigest"],
        "parentComponentArtifactDigests": parent["componentArtifactDigests"],
        "maintenanceGateGeneration": 1,
        "replacementReleaseId": "preview-sandboxd-fixed-release",
    }
    monkeypatch.setattr(successor.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        successor, "_parent_context", lambda *_args: (selector, parent, transaction)
    )
    monkeypatch.setattr(
        core,
        "_catalog_core_component_ids",
        lambda _updater: tuple(core.CORE_COMPONENT_IDS),
        raising=False,
    )
    monkeypatch.setattr(
        core, "_component_cohort", lambda _updater, values: tuple(values), raising=False
    )
    monkeypatch.setattr(successor, "_existing_attempt", lambda *_args: None)
    monkeypatch.setattr(
        successor,
        "_exact_successor_candidates",
        lambda *_args: (_ for _ in ()).throw(ValueError("signature verification failed")),
    )
    with pytest.raises(ValueError, match="signature verification failed"):
        successor.check(CandidateUpdater(), selector, channel="preview", core=core)
    assert calls == []
    assert transaction["phase"] == "hold_required"
    assert transaction.get("successorAttempts", []) == []


def _held_successor_fixture(
    tmp_path: Path,
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build private parent/child plan state without contacting a live Broker."""

    updater = fixtures.FakeUpdater(tmp_path)
    parent = fixtures._check_plan(updater, tmp_path)
    fixtures.bootstrap.stage(updater, parent["planId"], parent["planDigest"])
    old_artifacts = parent["componentArtifactDigests"]
    source_commit = "a" * 40
    items = [
        {
            "componentId": component_id,
            "version": "2.0.0" if component_id == "cyrene-sandboxd" else "1.0.0",
            "manifestDigest": _digest((component_id + "-successor-manifest").encode())
            if component_id == "cyrene-sandboxd"
            else next(
                item["manifestDigest"]
                for item in parent["components"]
                if item["componentId"] == component_id
            ),
            "artifactDigest": _digest((component_id + "-successor-artifact").encode())
            if component_id == "cyrene-sandboxd"
            else old_artifacts[component_id],
            "sourceCommit": "b" * 40 if component_id == "cyrene-sandboxd" else source_commit,
            "restartGroup": "core-runtime",
            "releaseId": "preview-"
            + ("b" * 40 if component_id == "cyrene-sandboxd" else source_commit),
        }
        for component_id in core_runtime.CORE_COMPONENT_IDS
    ]
    normal = {
        "planId": "plan-" + "c" * 32,
        "planDigest": _digest(b"successor base"),
        "channel": parent["channel"],
        "catalogGeneration": updater.catalog_generation,
        "catalogDigest": updater.catalog_digest,
        "components": items,
        "replacementReleaseId": "preview-" + "b" * 40,
    }
    transaction = {
        "schemaVersion": 1,
        "phase": "hold_required",
        "planId": parent["planId"],
        "planDigest": parent["planDigest"],
        "requestId": parent["requestId"],
        "componentArtifactDigests": old_artifacts,
        "beginRequest": core_runtime._core_bootstrap_begin_request(parent),
        "maintenanceGateGeneration": 10,
        "maintenanceToken": "t" * 40,
        "successorAttempts": [],
    }
    snapshot = {
        "gateGeneration": 10,
        "catalogGeneration": parent["catalogGeneration"],
        "activitySources": parent["activitySources"],
        "coreBootstrapEligible": False,
    }
    plan = successor.build_plan(
        core_runtime,
        parent,
        transaction,
        normal,
        snapshot,
        parent["targetId"],
        _canonical,
    )
    selector = successor.selector_for_plan(plan)
    attempt = {
        "schemaVersion": 1,
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "basePlanId": plan["basePlanId"],
        "basePlanDigest": plan["basePlanDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "successorBinding": plan["successorBinding"],
        "phase": "staged",
    }
    transaction["successorAttempts"] = [attempt]
    path = core_runtime._journal_path(updater)
    core_runtime._write_private_json(updater, path, transaction)
    core_runtime._write_private_json(
        updater, core_runtime._plan_path(updater, plan["planId"]), plan
    )
    base_plan = {
        "schemaVersion": 1,
        "channel": plan["channel"],
        "catalogGeneration": updater.catalog_generation,
        "catalogDigest": updater.catalog_digest,
        "components": [
            {
                key: item[key]
                for key in (
                    "componentId",
                    "version",
                    "manifestDigest",
                    "artifactDigest",
                    "restartGroup",
                )
            }
            for item in items
        ],
        "planId": normal["planId"],
        "planDigest": normal["planDigest"],
        "phase": "checked",
    }
    staged_components = [
        {
            **item,
            "manifest": {
                "componentId": item["componentId"],
                "version": item["version"],
                "manifestDigest": item["manifestDigest"],
                "artifact": {"sha256": item["artifactDigest"]},
                "source": {"commit": item["sourceCommit"]},
                "releaseId": item["releaseId"],
            },
            "releasePath": "/tmp/verified-release",
        }
        for item in items
    ]
    base_path = Path(updater.state_root) / "staged" / plan["basePlanId"] / "stage.json"
    core_runtime._write_private_json(
        updater,
        base_path,
        {
            "schemaVersion": 2,
            "plan": base_plan,
            "channel": plan["channel"],
            "components": staged_components,
            "phase": "staged",
        },
    )
    core_runtime._write_private_json(
        updater,
        Path(updater.state_root) / "staged" / plan["planId"] / "stage.json",
        {
            "phase": "staged",
            "plan": {**plan, "phase": "staged"},
            "successorBinding": plan["successorBinding"],
        },
    )
    return updater, parent, plan, selector


def test_successor_end_uncertain_retry_reuses_same_parent_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater, parent, plan, selector = _held_successor_fixture(tmp_path)
    monkeypatch.setattr(core_runtime, "_is_root", lambda: True)
    monkeypatch.setattr(core_runtime, "_verify_held_core_bootstrap", lambda *_args: None)
    monkeypatch.setattr(
        core_runtime,
        "_health_snapshot",
        lambda *_args, **_kwargs: {
            "gateGeneration": selector["maintenanceGateGeneration"],
            "catalogGeneration": parent["catalogGeneration"],
            "activitySources": parent["activitySources"],
            "coreBootstrapEligible": False,
        },
    )
    monkeypatch.setattr(core_runtime, "_assert_fresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        core_runtime, "_quiesce_hold_recovery_units", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(core_runtime, "_write_unit", lambda *_args: None)
    monkeypatch.setattr(core_runtime, "_activate_core_cohort", lambda *_args: None)
    monkeypatch.setattr(core_runtime, "_start_core_components", lambda *_args: None)
    monkeypatch.setattr(core_runtime, "_verify_live_core_cohort", lambda *_args: None)
    monkeypatch.setattr(core_runtime, "_require_core_ready", lambda *_args: None)
    monkeypatch.setattr(updater, "_validate_staged_record", lambda *_args, **_kwargs: None)
    end_ids: list[str | None] = []
    original_request = updater._broker_request

    def uncertain_once(
        method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        if method == "EndMaintenance":
            end_ids.append(request_id)
            if len(end_ids) == 1:
                raise RuntimeError("response lost after End dispatch")
            return {"unlocked": True, "status": "READY", "gate_generation": 11}
        return original_request(method, params, request_id=request_id)

    monkeypatch.setattr(updater, "_broker_request", uncertain_once)
    confirmation = successor.confirmation(plan)
    with pytest.raises(RuntimeError, match="result is uncertain"):
        core_runtime.apply(
            updater,
            plan["planId"],
            plan["planDigest"],
            confirmation,
            held_recovery=selector,
        )
    journal_path = core_runtime._journal_path(updater)
    pending = core_runtime._read_private_json(journal_path)
    assert pending["phase"] == "hold_required"
    assert pending["beginRequest"] == core_runtime._core_bootstrap_begin_request(parent)
    assert pending["successorAttempts"][0]["phase"] == "end_call_pending"
    result = core_runtime.apply(
        updater,
        plan["planId"],
        plan["planDigest"],
        confirmation,
        held_recovery=selector,
    )
    final = core_runtime._read_private_json(journal_path)
    assert result["status"] == "installed"
    assert final["phase"] == "successor_succeeded"
    assert final["gateReleaseConfirmed"] is True
    assert len(end_ids) == 2
    assert end_ids[0] == end_ids[1]


def test_exact_release_check_stage_apply_cleans_orphan_and_proves_ready_before_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = fixtures.FakeUpdater(tmp_path)
    parent = fixtures.bootstrap.check(
        updater, channel="preview", proc_root=fixtures._fake_proc(tmp_path / "proc-parent")
    )["plan"]
    fixtures.bootstrap.stage(updater, parent["planId"], parent["planDigest"])
    old_artifacts = parent["componentArtifactDigests"]
    parent_stage_path = Path(updater.state_root) / "staged" / parent["basePlanId"] / "stage.json"
    parent_stage = json.loads(parent_stage_path.read_text())
    candidate_by_tag: dict[tuple[str, str], Any] = {}
    for component_id in core_runtime.CORE_COMPONENT_IDS:
        old_item = next(
            item for item in parent["components"] if item["componentId"] == component_id
        )
        old_commit = "a" * 40
        new_item = {
            "componentId": component_id,
            "version": "2.0.0" if component_id == "cyrene-sandboxd" else old_item["version"],
            "manifestDigest": _digest((component_id + "-successor-manifest").encode())
            if component_id == "cyrene-sandboxd"
            else old_item["manifestDigest"],
            "artifactDigest": _digest((component_id + "-successor-artifact").encode())
            if component_id == "cyrene-sandboxd"
            else old_artifacts[component_id],
            "sourceCommit": "b" * 40 if component_id == "cyrene-sandboxd" else old_commit,
        }
        release_id = "preview-" + new_item["sourceCommit"]
        manifest = {
            "componentId": component_id,
            "version": new_item["version"],
            "manifestDigest": new_item["manifestDigest"],
            "releaseId": release_id,
            "channel": "preview",
            "target": updater.targets["target-ubuntu"]["target"],
            "artifact": {"sha256": new_item["artifactDigest"]},
            "source": {"commit": new_item["sourceCommit"]},
        }
        staged_item = next(
            item for item in parent_stage["components"] if item["componentId"] == component_id
        )
        staged_item["manifest"].update(
            {
                "channel": parent["channel"],
                "target": manifest["target"],
                "source": {"commit": old_commit},
                "artifact": {
                    **staged_item["manifest"]["artifact"],
                    "sha256": old_artifacts[component_id],
                },
            }
        )
        candidate_by_tag[(component_id, release_id)] = SimpleNamespace(
            component=updater.components[component_id],
            manifest=manifest,
            manifest_digest=new_item["manifestDigest"],
            artifact_digest=new_item["artifactDigest"],
            release_tag=release_id,
        )
    parent_stage_path.write_text(json.dumps(parent_stage), encoding="utf-8")
    parent_stage_path.chmod(0o600)
    transaction = {
        "schemaVersion": 1,
        "phase": "hold_required",
        "planId": parent["planId"],
        "planDigest": parent["planDigest"],
        "requestId": parent["requestId"],
        "componentArtifactDigests": parent["componentArtifactDigests"],
        "beginRequest": core_runtime._core_bootstrap_begin_request(parent),
        "maintenanceGateGeneration": 10,
        "maintenanceToken": "t" * 40,
        "successorAttempts": [],
    }
    core_runtime._write_private_json(updater, core_runtime._journal_path(updater), transaction)
    held_snapshot = {
        "gateGeneration": 10,
        "catalogGeneration": parent["catalogGeneration"],
        "activitySources": parent["activitySources"],
        "coreBootstrapEligible": False,
    }
    monkeypatch.setattr(core_runtime, "_verify_held_core_bootstrap", lambda *_args: None)
    monkeypatch.setattr(core_runtime, "_health_snapshot", lambda *_args, **_kwargs: held_snapshot)
    monkeypatch.setattr(core_runtime, "_is_root", lambda: True)
    monkeypatch.setattr(successor, "_is_root", lambda _updater: True)
    monkeypatch.setattr(
        updater, "_component_release_tag_prefix", lambda *_args: None, raising=False
    )
    monkeypatch.setattr(
        updater, "_manifest_target_id", lambda *_args: "target-ubuntu", raising=False
    )
    monkeypatch.setattr(updater, "_validate_manifest_digest", lambda *_args: None, raising=False)
    selected_release_ids: list[str] = []

    def select_candidate(
        component: dict[str, Any], _target: dict[str, Any], _channel: str, *, release_id: str
    ) -> Any:
        selected_release_ids.append(release_id)
        candidate = candidate_by_tag.get((component["componentId"], release_id))
        if candidate is None or candidate.component["componentId"] != component["componentId"]:
            raise ValueError("signed release candidate unavailable")
        return candidate

    monkeypatch.setattr(updater, "_candidate", select_candidate, raising=False)
    monkeypatch.setattr(
        updater,
        "_expand_compatibility_groups",
        lambda candidates, _channel: candidates,
        raising=False,
    )
    monkeypatch.setattr(
        updater, "_validate_runtime_dependencies", lambda _candidates: None, raising=False
    )
    monkeypatch.setattr(
        updater, "_placement_bindings", lambda *_args, **_kwargs: None, raising=False
    )
    monkeypatch.setattr(
        updater, "_revalidate_plan_placement", lambda *_args, **_kwargs: None, raising=False
    )
    monkeypatch.setattr(
        updater,
        "_private_state_directory",
        lambda kind: _private_state_dir(updater, kind),
        raising=False,
    )
    monkeypatch.setattr(updater, "_validate_staged_record", lambda *_args, **_kwargs: None)

    def stage_candidate(
        candidate: Any, _plan_root: Path, _plan_id: str, _plan_digest: str
    ) -> dict[str, Any]:
        component_id = candidate.component["componentId"]
        return {
            "componentId": component_id,
            "version": candidate.manifest["version"],
            "manifestDigest": candidate.manifest_digest,
            "releaseIdentity": candidate.manifest_digest,
            "artifactDigest": candidate.artifact_digest,
            "restartGroup": "core-runtime",
            "pointerIdentity": candidate.manifest["version"]
            + "--"
            + candidate.manifest_digest.removeprefix("sha256:"),
            "bundleIdentity": None,
            "manifest": candidate.manifest,
            "releasePath": "/tmp/verified-release",
            "archivePath": "/tmp/verified-archive",
        }

    monkeypatch.setattr(updater, "_stage_candidate", stage_candidate, raising=False)
    selector = {
        "schemaVersion": 1,
        "mode": "held-successor",
        "parentPlanId": parent["planId"],
        "parentPlanDigest": parent["planDigest"],
        "parentComponentArtifactDigests": parent["componentArtifactDigests"],
        "maintenanceGateGeneration": 10,
        "replacementReleaseId": "preview-" + "b" * 40,
    }
    checked = successor.check(updater, selector, channel="preview", core=core_runtime)
    plan = checked["plan"]
    assert plan["successorBinding"]["replacementReleaseId"] == selector["replacementReleaseId"]
    assert selected_release_ids == [
        "preview-" + ("b" * 40 if component == "cyrene-sandboxd" else "a" * 40)
        for component in core_runtime.CORE_COMPONENT_IDS
    ]
    staged = successor.stage(
        updater, plan["planId"], plan["planDigest"], selector, channel="preview", core=core_runtime
    )
    assert staged["status"] == "staged"
    staged_components = core_runtime._read_private_json(
        Path(updater.state_root) / "staged" / plan["basePlanId"] / "stage.json"
    )["components"]
    expected_by_id = {item["componentId"]: item for item in plan["components"]}
    for staged_item in staged_components:
        selected = expected_by_id[staged_item["componentId"]]
        assert staged_item["manifest"].get("releaseId") == selected["releaseId"]
        assert staged_item["manifest"]["source"]["commit"] == selected["sourceCommit"]
        assert staged_item["manifestDigest"] == selected["manifestDigest"]
    selected_once = [
        "preview-" + ("b" * 40 if component == "cyrene-sandboxd" else "a" * 40)
        for component in core_runtime.CORE_COMPONENT_IDS
    ]
    assert selected_release_ids == [*selected_once, *selected_once]

    order: list[str] = []
    monkeypatch.setattr(core_runtime, "_assert_fresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        core_runtime,
        "_quiesce_hold_recovery_units",
        lambda *_args, **_kwargs: order.append("orphan-quiesced"),
    )
    monkeypatch.setattr(core_runtime, "_write_unit", lambda *_args: None)
    monkeypatch.setattr(
        core_runtime, "_activate_core_cohort", lambda *_args: order.append("cohort-active")
    )
    monkeypatch.setattr(
        core_runtime, "_start_core_components", lambda *_args: order.append("cohort-started")
    )
    monkeypatch.setattr(
        updater, "_health_transaction", lambda *_args: order.append("cohort-health")
    )
    monkeypatch.setattr(
        core_runtime, "_verify_live_core_cohort", lambda *_args: order.append("live-cohort-proof")
    )
    monkeypatch.setattr(
        core_runtime, "_require_core_ready", lambda *_args: order.append("full-cohort-ready")
    )
    original_request = updater._broker_request

    def record_end(
        method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        if method == "EndMaintenance":
            order.append("end")
        return original_request(method, params, request_id=request_id)

    monkeypatch.setattr(updater, "_broker_request", record_end)
    result = core_runtime.apply(
        updater,
        plan["planId"],
        plan["planDigest"],
        successor.confirmation(plan),
        held_recovery=selector,
    )
    assert result["status"] == "installed"
    assert order.index("orphan-quiesced") < order.index("cohort-started")
    assert order.index("full-cohort-ready") < order.index("end")


def test_original_apply_cannot_bypass_an_appended_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = fixtures.FakeUpdater(tmp_path)
    parent = fixtures._check_plan(updater, tmp_path)
    fixtures.bootstrap.stage(updater, parent["planId"], parent["planDigest"])
    journal = {
        "phase": "hold_required",
        "planId": parent["planId"],
        "planDigest": parent["planDigest"],
        "requestId": parent["requestId"],
        "successorAttempts": [{"planId": "plan-" + "d" * 32}],
    }
    core_runtime._write_private_json(updater, core_runtime._journal_path(updater), journal)
    monkeypatch.setattr(core_runtime, "_is_root", lambda: True)
    confirmation = {
        "mode": core_runtime.CORE_BOOTSTRAP_MODE,
        "planId": parent["planId"],
        "planDigest": parent["planDigest"],
        "componentArtifactDigests": parent["componentArtifactDigests"],
        "catalogGeneration": parent["catalogGeneration"],
        "gateGeneration": parent["gateGeneration"],
        "confirmed": True,
    }
    with pytest.raises(ValueError, match="successor owns recovery"):
        core_runtime.apply(updater, parent["planId"], parent["planDigest"], confirmation)
