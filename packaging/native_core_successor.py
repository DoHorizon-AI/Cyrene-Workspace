"""Explicit successor planning for a failed held first-Core cohort.

The original plan and Begin authority remain immutable while a signed Core-only
successor is checked and staged under the same durable hold.
中文：仅在原始持久闭门下显式生成并暂存签名 Core 后继计划。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

SUCCESSOR_MODE = "held-successor"
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PLAN_ID_PATTERN = re.compile(r"plan-[0-9a-f]{32}\Z")
_SELECTOR_KEYS = frozenset(
    {
        "schemaVersion",
        "mode",
        "parentPlanId",
        "parentPlanDigest",
        "parentComponentArtifactDigests",
        "maintenanceGateGeneration",
        "replacementReleaseId",
    }
)


def _require_digest(value: Any, field: str) -> str:
    """Require a canonical SHA-256 string; reject booleans and coercions."""

    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical SHA-256 digest")
    return value


def validate_selector(value: Any) -> dict[str, Any]:
    """Validate the exact operator-selected parent tuple for held recovery.

    Args:
        value: The request's `heldRecovery` object.
    Returns:
        A detached, validated selector dictionary.
    Raises:
        TypeError or ValueError: If the selector is malformed or incomplete.
    """

    if not isinstance(value, dict) or set(value) != _SELECTOR_KEYS:
        raise ValueError("heldRecovery must contain exactly the supported parent selector fields")
    if value.get("schemaVersion") != 1 or value.get("mode") != SUCCESSOR_MODE:
        raise ValueError("Unsupported heldRecovery selector version or mode")
    parent_id = value.get("parentPlanId")
    if not isinstance(parent_id, str) or _PLAN_ID_PATTERN.fullmatch(parent_id) is None:
        raise ValueError("heldRecovery parentPlanId is malformed")
    _require_digest(value.get("parentPlanDigest"), "heldRecovery parentPlanDigest")
    artifacts = value.get("parentComponentArtifactDigests")
    if (
        not isinstance(artifacts, dict)
        or not artifacts
        or any(not isinstance(key, str) or not key for key in artifacts)
        or any(
            _DIGEST_PATTERN.fullmatch(item) is None
            for item in artifacts.values()
            if isinstance(item, str)
        )
        or any(not isinstance(item, str) for item in artifacts.values())
    ):
        raise ValueError("heldRecovery parent artifact map is malformed")
    generation = value.get("maintenanceGateGeneration")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("heldRecovery maintenanceGateGeneration must be a positive integer")
    replacement_release_id = value.get("replacementReleaseId")
    if (
        not isinstance(replacement_release_id, str)
        or not replacement_release_id
        or len(replacement_release_id) > 256
        or "/" in replacement_release_id
        or ".." in replacement_release_id
    ):
        raise ValueError("heldRecovery replacementReleaseId must select one immutable release")
    return {
        "schemaVersion": 1,
        "mode": SUCCESSOR_MODE,
        "parentPlanId": parent_id,
        "parentPlanDigest": value["parentPlanDigest"],
        "parentComponentArtifactDigests": dict(sorted(artifacts.items())),
        "maintenanceGateGeneration": generation,
        "replacementReleaseId": replacement_release_id,
    }


def _successor_binding(
    parent_plan: dict[str, Any],
    transaction: dict[str, Any],
    normal_plan: dict[str, Any],
    canonical_json: Any,
) -> dict[str, Any]:
    """Derive the digest-bound parent authority tuple from protected state."""

    begin_request = transaction.get("beginRequest")
    if not isinstance(begin_request, dict):
        raise TypeError("Held parent journal lacks its original Begin authority request")
    encoded = canonical_json(begin_request)
    if not isinstance(encoded, bytes):
        raise TypeError("Canonical JSON encoder must return bytes")
    return {
        "schemaVersion": 1,
        "mode": SUCCESSOR_MODE,
        "parentPlanId": parent_plan["planId"],
        "parentPlanDigest": parent_plan["planDigest"],
        "parentRequestId": parent_plan["requestId"],
        "parentComponentArtifactDigests": dict(
            sorted(parent_plan["componentArtifactDigests"].items())
        ),
        "parentBeginRequestDigest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "maintenanceGateGeneration": transaction["maintenanceGateGeneration"],
        "candidateCatalogGeneration": normal_plan["catalogGeneration"],
        "candidateCatalogDigest": normal_plan["catalogDigest"],
        "replacementReleaseId": normal_plan["replacementReleaseId"],
    }


def plan_material(plan: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the canonical immutable material for a successor plan."""

    keys = (
        "schemaVersion",
        "mode",
        "basePlanId",
        "basePlanDigest",
        "channel",
        "catalogGeneration",
        "catalogDigest",
        "gateGeneration",
        "activitySources",
        "coreBootstrapEligible",
        "targetId",
        "components",
        "includeProducts",
    )
    if any(key not in plan for key in keys):
        raise ValueError("Held successor plan is missing immutable material")
    binding = plan.get("successorBinding")
    if not isinstance(binding, dict):
        raise TypeError("Held successor plan has no parent binding")
    material = {**{key: plan[key] for key in keys}, "successorBinding": binding}
    if "bootstrapBroker" in plan:
        material["bootstrapBroker"] = plan["bootstrapBroker"]
    return material


def build_plan(
    core: Any,
    parent_plan: dict[str, Any],
    transaction: dict[str, Any],
    normal_plan: dict[str, Any],
    held_snapshot: dict[str, Any],
    target_id: str,
    canonical_json: Any,
) -> dict[str, Any]:
    """Build a canonical Core-only successor bound to a confirmed held parent.

    The current held gate generation is recorded separately from the parent's
    pre-Begin generation. The signed candidate's catalog and sources must remain
    compatible with the original Begin request.
    中文：保留原始确认代数，并单独绑定当前闭门代数。
    """

    if parent_plan.get("includeProducts", False) is not False or "firstProducts" in parent_plan:
        raise ValueError("Held successors currently support only the Core cohort")
    if transaction.get("phase") != "hold_required":
        raise ValueError("A successor can only be created from the exact held failure phase")
    expected_ids = set(core.CORE_COMPONENT_IDS)
    parent_ids = set(parent_plan.get("componentArtifactDigests", {}))
    candidate_items = normal_plan.get("components")
    candidate_ids = (
        {item.get("componentId") for item in candidate_items if isinstance(item, dict)}
        if isinstance(candidate_items, list)
        else set()
    )
    if parent_ids != expected_ids or candidate_ids != expected_ids:
        raise ValueError("Held successor must preserve the exact four-component Core cohort")
    previous_items = parent_plan.get("components")
    if not isinstance(previous_items, list):
        raise TypeError("Held parent plan lacks its signed four-component identity")
    previous_by_id = {
        item.get("componentId"): item for item in previous_items if isinstance(item, dict)
    }
    candidate_by_id = {
        item.get("componentId"): item for item in candidate_items if isinstance(item, dict)
    }
    if set(previous_by_id) != expected_ids:
        raise ValueError("Held parent plan does not describe the exact legacy C9 cohort")
    unchanged_ids = expected_ids - {"cyrene-sandboxd"}
    identity_fields = ("version", "manifestDigest", "artifactDigest")
    for component_id in unchanged_ids:
        previous = previous_by_id[component_id]
        candidate = candidate_by_id[component_id]
        if any(previous.get(field) != candidate.get(field) for field in identity_fields):
            raise ValueError(
                f"Held successor may replace only cyrene-sandboxd; {component_id} identity changed"
            )
    if previous_by_id["cyrene-sandboxd"].get("artifactDigest") == candidate_by_id[
        "cyrene-sandboxd"
    ].get("artifactDigest"):
        raise ValueError("Held successor must bind the signed cyrene-sandboxd replacement artifact")
    if held_snapshot.get("gateGeneration") != transaction.get("maintenanceGateGeneration"):
        raise ValueError("Live held gate generation differs from the protected parent journal")
    if held_snapshot.get("catalogGeneration") != parent_plan.get("catalogGeneration"):
        raise ValueError("Live held catalog generation differs from the protected parent plan")
    if held_snapshot.get("activitySources") != parent_plan.get("activitySources"):
        raise ValueError("Live activity sources differ from the protected parent plan")
    if held_snapshot.get("coreBootstrapEligible") is not False:
        raise ValueError("Successor planning requires the live non-fresh held-state observation")
    if not isinstance(target_id, str) or not target_id or target_id != parent_plan.get("targetId"):
        raise ValueError("Successor Core artifacts do not share one supported target")
    sorted_items = sorted(candidate_items, key=lambda item: item["componentId"])
    material = {
        "schemaVersion": 1,
        "mode": core.CORE_BOOTSTRAP_MODE,
        "basePlanId": normal_plan["planId"],
        "basePlanDigest": normal_plan["planDigest"],
        "channel": normal_plan["channel"],
        "catalogGeneration": parent_plan["catalogGeneration"],
        "catalogDigest": normal_plan["catalogDigest"],
        "gateGeneration": parent_plan["gateGeneration"],
        "activitySources": parent_plan["activitySources"],
        "coreBootstrapEligible": False,
        "targetId": target_id,
        "components": sorted_items,
        "includeProducts": False,
        "successorBinding": _successor_binding(
            parent_plan, transaction, normal_plan, canonical_json
        ),
    }
    if "bootstrapBroker" in parent_plan:
        material["bootstrapBroker"] = parent_plan["bootstrapBroker"]
    encoded = canonical_json(material)
    if not isinstance(encoded, bytes):
        raise TypeError("Canonical JSON encoder must return bytes")
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    suffix = digest.removeprefix("sha256:")
    return {
        **material,
        "planId": "plan-" + suffix[:32],
        "planDigest": digest,
        "requestId": "bootstrap-" + suffix[:32],
        "phase": "checked",
        "requiresRestart": True,
        "userConfirmedRestart": True,
        "componentArtifactDigests": {
            item["componentId"]: item["artifactDigest"] for item in sorted_items
        },
    }


def selector_for_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the exact public selector matching a digest-bound successor plan."""

    binding = plan.get("successorBinding")
    if not isinstance(binding, dict):
        raise TypeError("Plan has no held-successor binding")
    return validate_selector(
        {
            "schemaVersion": binding.get("schemaVersion"),
            "mode": binding.get("mode"),
            "parentPlanId": binding.get("parentPlanId"),
            "parentPlanDigest": binding.get("parentPlanDigest"),
            "parentComponentArtifactDigests": binding.get("parentComponentArtifactDigests"),
            "maintenanceGateGeneration": binding.get("maintenanceGateGeneration"),
            "replacementReleaseId": binding.get("replacementReleaseId"),
        }
    )


def confirmation(plan: dict[str, Any]) -> dict[str, Any]:
    """Build explicit confirmation data for exactly one staged successor plan."""

    result = {
        "mode": plan["mode"],
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
        "includeProducts": False,
        "successorBinding": plan["successorBinding"],
    }
    if "bootstrapBroker" in plan:
        result["bootstrapBroker"] = plan["bootstrapBroker"]
    return result


def _canonical_json(updater: Any) -> Any:
    """Find the canonical JCS encoder from the loaded trusted updater module."""

    module = sys.modules.get(updater.__class__.__module__)
    canonical = getattr(module, "canonical_jcs", None)
    if not callable(canonical):
        raise TypeError("Trusted plan canonicalization is unavailable")
    return canonical


def _selector_matches_parent(
    selector: dict[str, Any], parent: dict[str, Any], held_gen: Any
) -> bool:
    """Compare user selection with the exact stored parent plan and active hold."""

    return (
        selector["parentPlanId"] == parent.get("planId")
        and selector["parentPlanDigest"] == parent.get("planDigest")
        and selector["parentComponentArtifactDigests"] == parent.get("componentArtifactDigests")
        and selector["maintenanceGateGeneration"] == held_gen
    )


def _parent_context(
    updater: Any, selector_value: Any, core: Any
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load and validate the original plan and journal without issuing Broker RPCs."""

    selector = validate_selector(selector_value)
    parent = core._load_plan(updater, selector["parentPlanId"], selector["parentPlanDigest"])
    journal_path = core._journal_path(updater)
    transaction = core._read_private_json(journal_path)
    if not isinstance(transaction, dict) or transaction.get("phase") != "hold_required":
        raise ValueError("Held successor requires the original hold_required journal")
    if not _selector_matches_parent(selector, parent, transaction.get("maintenanceGateGeneration")):
        raise ValueError("heldRecovery selector differs from the immutable parent plan or hold")
    if (
        parent.get("includeProducts", False) is not False
        or "firstProducts" in parent
        or transaction.get("includeProducts", False) is not False
        or "firstProducts" in transaction
    ):
        raise ValueError("Held successor currently supports only a Core-only parent")
    if (
        transaction.get("planId") != parent["planId"]
        or transaction.get("planDigest") != parent["planDigest"]
        or transaction.get("requestId") != parent["requestId"]
        or transaction.get("componentArtifactDigests") != parent["componentArtifactDigests"]
        or transaction.get("beginRequest") != core._core_bootstrap_begin_request(parent)
        or transaction.get("gateReleaseConfirmed") is True
    ):
        raise ValueError("Held parent journal no longer matches its original Begin authority")
    attempts = transaction.get("successorAttempts", [])
    if not isinstance(attempts, list) or len(attempts) > 1:
        raise ValueError("Held parent journal has an unsupported successor-attempt history")
    return selector, parent, transaction


def _confirm_parent_hold(
    updater: Any,
    selector: dict[str, Any],
    parent: dict[str, Any],
    transaction: dict[str, Any],
    core: Any,
) -> dict[str, Any]:
    """Replay only the original Begin authority and confirm the exact live hold."""

    core._verify_held_core_bootstrap(updater, parent, transaction)
    snapshot = core._health_snapshot(updater, require_eligible=False)
    if (
        snapshot["gateGeneration"] != selector["maintenanceGateGeneration"]
        or snapshot["catalogGeneration"] != parent["catalogGeneration"]
        or snapshot["activitySources"] != parent["activitySources"]
        or snapshot["coreBootstrapEligible"] is not False
    ):
        raise ValueError("Live Broker state does not confirm the exact existing closed hold")
    return snapshot


def _read_successor_plan(updater: Any, plan_id: Any, plan_digest: Any, core: Any) -> dict[str, Any]:
    """Load and digest-check a plan whose parent lineage is part of its material."""

    updater._validate_plan_identity(plan_id, plan_digest)
    plan = core._read_private_json(core._plan_path(updater, plan_id))
    if (
        not isinstance(plan, dict)
        or plan.get("planId") != plan_id
        or plan.get("planDigest") != plan_digest
        or plan.get("mode") != core.CORE_BOOTSTRAP_MODE
        or plan.get("includeProducts") is not False
    ):
        raise ValueError("Held successor plan identity or Core-only profile changed")
    canonical = _canonical_json(updater)
    material = plan_material(plan)
    digest = "sha256:" + hashlib.sha256(canonical(material)).hexdigest()
    if digest != plan_digest or plan_id != "plan-" + digest.split(":", 1)[1][:32]:
        raise ValueError("Held successor digest does not match its immutable lineage material")
    expected = {item["componentId"]: item["artifactDigest"] for item in plan["components"]}
    if plan.get("componentArtifactDigests") != expected:
        raise ValueError("Held successor artifact map differs from its immutable cohort")
    return plan


def _existing_attempt(
    transaction: dict[str, Any], selector: dict[str, Any]
) -> dict[str, Any] | None:
    """Return only the exact already-bound child attempt, if present."""

    attempts = transaction.get("successorAttempts", [])
    if not attempts:
        return None
    attempt = attempts[0]
    if not isinstance(attempt, dict) or attempt.get("successorBinding") is None:
        raise ValueError("Held successor attempt history is malformed")
    if selector_for_plan({"successorBinding": attempt["successorBinding"]}) != selector:
        raise ValueError("A different successor attempt is already bound to the held parent")
    return attempt


def _write_attempt(
    updater: Any,
    core: Any,
    journal_path: Path,
    transaction: dict[str, Any],
    attempt: dict[str, Any],
) -> None:
    """Append or advance one child record without rewriting parent authority fields."""

    current = core._read_private_json(journal_path)
    if not isinstance(current, dict):
        raise TypeError("Held parent journal disappeared before successor progress was recorded")
    immutable = ("planId", "planDigest", "requestId", "componentArtifactDigests", "beginRequest")
    if any(current.get(key) != transaction.get(key) for key in immutable):
        raise ValueError("Held parent authority changed while recording its successor")
    attempts = current.setdefault("successorAttempts", [])
    if not isinstance(attempts, list) or len(attempts) > 1:
        raise ValueError("Held parent successor history is malformed")
    if attempts:
        if attempts[0].get("planId") != attempt.get("planId"):
            raise ValueError("A different successor attempt is already bound to the held parent")
        attempts[0] = attempt
    else:
        attempts.append(attempt)
    core._write_private_json(updater, journal_path, current)


def check(updater: Any, selector_value: Any, *, channel: Any, core: Any) -> dict[str, Any]:
    """Check signed replacement Core artifacts under an already-proven durable hold."""

    if not _is_root(updater):
        raise PermissionError("Held successor planning requires the root-authorized updater")
    selector = validate_selector(selector_value)
    updater._reload_catalog_for_operation()
    updater._ensure_state_root()
    with updater._exclusive_update_lock():
        selector, parent, transaction = _parent_context(updater, selector, core)
        existing = _existing_attempt(transaction, selector)
        if existing is not None:
            _confirm_parent_hold(updater, selector, parent, transaction, core)
            plan = _read_successor_plan(
                updater, existing.get("planId"), existing.get("planDigest"), core
            )
            return {
                "status": "checked",
                "mode": core.CORE_BOOTSTRAP_MODE,
                "plan": plan,
                "plans": [plan],
            }
        if not isinstance(channel, str) or updater._resolve_channel(channel) != channel:
            raise ValueError("Held successor check requires an explicit supported channel")
        cohort = tuple(core.CORE_COMPONENT_IDS)
        if tuple(core._catalog_core_component_ids(updater)) != cohort:
            raise ValueError("Held successor requires the trusted legacy C9 four-member cohort")
        core._component_cohort(updater, set(cohort))
        candidates = _exact_successor_candidates(updater, selector, parent, channel, cohort, core)
        candidates = updater._expand_compatibility_groups(candidates, channel)
        if set(candidates) != set(cohort):
            raise ValueError("Trusted compatibility expansion changes the fixed C9 Core cohort")
        updater._validate_runtime_dependencies(candidates)
        items = [
            _candidate_plan_item(component_id, candidates[component_id]) for component_id in cohort
        ]
        placement = updater._placement_bindings(candidates, operation="check")
        candidate_plan = {
            "channel": channel,
            "catalogGeneration": updater.catalog_generation,
            "catalogDigest": updater.catalog_digest,
            "components": items,
            "replacementReleaseId": selector["replacementReleaseId"],
            **({"deploymentPlacement": placement} if placement else {}),
        }
        base_plan = _base_plan(updater, candidate_plan)
        normal = {**base_plan, **candidate_plan}
        targets = {
            updater._target_for(updater.components[component_id]).get("id")
            for component_id in cohort
        }
        if len(targets) != 1 or None in targets:
            raise ValueError("Signed successor artifacts do not share one native target")
        # First verify the selected signed release and its exact Core profile. Only then
        # replay the original idempotent Begin payload to confirm authority for this hold.
        snapshot = _confirm_parent_hold(updater, selector, parent, transaction, core)
        plan = build_plan(
            core,
            parent,
            transaction,
            normal,
            snapshot,
            next(iter(targets)),
            _canonical_json(updater),
        )
        core._write_private_json(updater, core._plan_path(updater, plan["planId"]), plan)
        attempt = {
            "schemaVersion": 1,
            "planId": plan["planId"],
            "planDigest": plan["planDigest"],
            "basePlanId": plan["basePlanId"],
            "basePlanDigest": plan["basePlanDigest"],
            "componentArtifactDigests": plan["componentArtifactDigests"],
            "successorBinding": plan["successorBinding"],
            "phase": "checked",
        }
        _write_attempt(updater, core, core._journal_path(updater), transaction, attempt)
        return {
            "status": "checked",
            "mode": core.CORE_BOOTSTRAP_MODE,
            "plan": plan,
            "plans": [plan],
        }


def _is_root(updater: Any) -> bool:
    """Use the updater's host identity contract for privileged transitions."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return False
    if not hasattr(updater, "_require_authorized_process"):
        return False
    updater._require_authorized_process()
    return True


def _component_release_id(
    updater: Any, component: dict[str, Any], channel: str, commit: str
) -> str:
    """Derive the immutable publisher tag for a parent-pinned component identity."""

    prefix = updater._component_release_tag_prefix(component, channel)
    return (prefix or ("preview-" if channel == "preview" else "stable-")) + commit


def _parent_release_pins(
    updater: Any, parent: dict[str, Any], core: Any
) -> dict[str, dict[str, Any]]:
    """Read old release tags solely as proof of the immutable held cohort identity."""

    base_id = parent.get("basePlanId")
    base_digest = parent.get("basePlanDigest")
    if not isinstance(base_id, str) or not isinstance(base_digest, str):
        raise TypeError("Held parent has no original checked Core base plan")
    path = Path(updater.state_root) / "staged" / base_id / "stage.json"
    record = core._read_private_json(path)
    if (
        not isinstance(record, dict)
        or record.get("phase") != "staged"
        or not isinstance(record.get("plan"), dict)
        or not isinstance(record.get("components"), list)
    ):
        raise ValueError("Original held stage is unavailable as cohort identity evidence")
    base = record["plan"]
    if (
        base.get("planId") != base_id
        or base.get("planDigest") != base_digest
        or base.get("catalogDigest") != parent.get("catalogDigest")
        or base.get("channel") != parent.get("channel")
    ):
        raise ValueError("Original held stage no longer matches the immutable parent base plan")
    expected_material = {
        key: base[key]
        for key in ("schemaVersion", "channel", "catalogGeneration", "catalogDigest", "components")
    }
    if "deploymentPlacement" in base:
        expected_material["deploymentPlacement"] = base["deploymentPlacement"]
    expected_digest = (
        "sha256:" + hashlib.sha256(_canonical_json(updater)(expected_material)).hexdigest()
    )
    if expected_digest != base_digest or base_id != "plan-" + expected_digest.split(":", 1)[1][:32]:
        raise ValueError("Original held base plan digest is invalid")
    planned = {
        item.get("componentId"): item
        for item in parent.get("components", [])
        if isinstance(item, dict)
    }
    staged = {
        item.get("componentId"): item for item in record["components"] if isinstance(item, dict)
    }
    base_items = {
        item.get("componentId"): item
        for item in base.get("components", [])
        if isinstance(item, dict)
    }
    cohort = set(core.CORE_COMPONENT_IDS)
    if set(planned) != cohort or set(staged) != cohort or set(base_items) != cohort:
        raise ValueError("Original held stage does not prove the exact four-member C9 cohort")
    pins: dict[str, dict[str, Any]] = {}
    for component_id in cohort:
        item = staged[component_id]
        plan_item = planned[component_id]
        base_item = base_items[component_id]
        manifest = item.get("manifest")
        if not isinstance(manifest, dict):
            raise TypeError(f"Original held stage lacks its {component_id} signed manifest")
        updater._validate_manifest_digest(manifest, plan_item.get("manifestDigest"))
        source = manifest.get("source")
        source_commit = source.get("commit") if isinstance(source, dict) else None
        artifact = manifest.get("artifact")
        artifact_digest = (
            artifact.get("digest", artifact.get("sha256")) if isinstance(artifact, dict) else None
        )
        component = updater.components.get(component_id)
        if not isinstance(component, dict):
            raise TypeError(f"Current trusted catalog omits held component {component_id}")
        if (
            item.get("componentId") != component_id
            or item.get("version") != plan_item.get("version")
            or item.get("manifestDigest") != plan_item.get("manifestDigest")
            or item.get("artifactDigest") != plan_item.get("artifactDigest")
            or base_item.get("componentId") != component_id
            or base_item.get("version") != plan_item.get("version")
            or base_item.get("manifestDigest") != plan_item.get("manifestDigest")
            or base_item.get("artifactDigest") != plan_item.get("artifactDigest")
            or base_item != plan_item
            or manifest.get("componentId") != component_id
            or manifest.get("version") != plan_item.get("version")
            or artifact_digest != plan_item.get("artifactDigest")
            or not isinstance(source_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
            or manifest.get("channel") != parent.get("channel")
            or updater._manifest_target_id(component, manifest) != parent.get("targetId")
        ):
            raise ValueError(
                f"Original held stage does not prove the pinned {component_id} identity"
            )
        release_id = _component_release_id(updater, component, parent["channel"], source_commit)
        pins[component_id] = {**plan_item, "sourceCommit": source_commit, "releaseId": release_id}
    return pins


def _exact_successor_candidates(
    updater: Any,
    selector: dict[str, Any],
    parent: dict[str, Any],
    channel: str,
    cohort: tuple[str, ...],
    core: Any,
) -> dict[str, Any]:
    """Resolve each Core member by immutable release tag and prove the parent pins."""

    parent_items = _parent_release_pins(updater, parent, core)
    if set(parent_items) != set(cohort):
        raise ValueError("Held parent does not pin the exact C9 component identities")
    candidates: dict[str, Any] = {}
    for component_id in cohort:
        component = updater.components.get(component_id)
        target = updater._target_for(component) if isinstance(component, dict) else None
        if not isinstance(component, dict) or not isinstance(target, dict):
            raise TypeError(f"Trusted catalog no longer supports held component {component_id}")
        if component_id == "cyrene-sandboxd":
            release_id = selector["replacementReleaseId"]
        else:
            prior = parent_items[component_id]
            release_id = prior["releaseId"]
        candidates[component_id] = updater._candidate(
            component, target, channel, release_id=release_id
        )
    for component_id in set(cohort) - {"cyrene-sandboxd"}:
        prior = parent_items[component_id]
        candidate = candidates[component_id]
        source = candidate.manifest.get("source")
        if (
            candidate.manifest.get("version") != prior.get("version")
            or candidate.manifest_digest != prior.get("manifestDigest")
            or candidate.artifact_digest != prior.get("artifactDigest")
            or not isinstance(source, dict)
            or source.get("commit") != prior["sourceCommit"]
        ):
            raise ValueError(
                f"Exact successor release differs from original {component_id} identity"
            )
    replacement = candidates["cyrene-sandboxd"]
    if replacement.release_tag != selector["replacementReleaseId"]:
        raise ValueError("Selected sandboxd release tag differs from heldRecovery selector")
    return candidates


def _candidate_plan_item(component_id: str, candidate: Any) -> dict[str, Any]:
    """Project one fully verified candidate into the canonical plan identity."""

    source = candidate.manifest.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("commit"), str):
        raise TypeError(f"Verified release lacks source identity for {component_id}")
    return {
        "componentId": component_id,
        "version": candidate.manifest["version"],
        "manifestDigest": candidate.manifest_digest,
        "artifactDigest": candidate.artifact_digest,
        "sourceCommit": source["commit"],
        "restartGroup": candidate.component["restart"]["group"],
        "releaseId": candidate.release_tag,
    }


def _base_plan(updater: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Build the updater-native checked-plan envelope for exact selected candidates."""

    components = [
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
        for item in sorted(plan["components"], key=lambda value: value["componentId"])
    ]
    material = {
        "schemaVersion": 1,
        "channel": plan["channel"],
        "catalogGeneration": updater.catalog_generation,
        "catalogDigest": updater.catalog_digest,
        "components": components,
    }
    if "deploymentPlacement" in plan:
        material["deploymentPlacement"] = plan["deploymentPlacement"]
    canonical = _canonical_json(updater)
    digest = "sha256:" + hashlib.sha256(canonical(material)).hexdigest()
    return {
        **material,
        "planId": "plan-" + digest.split(":", 1)[1][:32],
        "planDigest": digest,
        "phase": "checked",
    }


def stage(
    updater: Any,
    plan_id: Any,
    plan_digest: Any,
    selector_value: Any,
    *,
    channel: Any,
    core: Any,
) -> dict[str, Any]:
    """Stage only the exact signed child already appended to the held parent journal."""

    with updater._exclusive_update_lock():
        return _stage_locked(
            updater, plan_id, plan_digest, selector_value, channel=channel, core=core
        )


def _stage_locked(
    updater: Any,
    plan_id: Any,
    plan_digest: Any,
    selector_value: Any,
    *,
    channel: Any,
    core: Any,
) -> dict[str, Any]:
    """Run successor staging while holding the updater transaction lock."""

    selector = validate_selector(selector_value)
    plan = _read_successor_plan(updater, plan_id, plan_digest, core)
    if selector_for_plan(plan) != selector:
        raise ValueError("Stage selector differs from the successor plan's digest-bound parent")
    selector, parent, transaction = _parent_context(updater, selector, core)
    _confirm_parent_hold(updater, selector, parent, transaction, core)
    attempt = _existing_attempt(transaction, selector)
    if (
        attempt is None
        or attempt.get("planId") != plan_id
        or attempt.get("planDigest") != plan_digest
        or attempt.get("successorBinding") != plan["successorBinding"]
    ):
        raise ValueError("Successor plan was not canonically appended to this held parent")
    if attempt.get("phase") in {
        "staged",
        "applying",
        "end_call_pending",
        "end_confirmed",
        "succeeded",
    }:
        record = core._read_private_json(
            Path(updater.state_root) / "staged" / plan_id / "stage.json"
        )
        if not isinstance(record, dict) or record.get("phase") != "staged":
            raise ValueError("Successor attempt claims staged without its exact stage receipt")
        return {
            "status": "staged",
            "mode": core.CORE_BOOTSTRAP_MODE,
            "plan": {**plan, "phase": "staged"},
            "plans": [{**plan, "phase": "staged"}],
            "components": record.get("components", []),
        }
    if attempt.get("phase") != "checked":
        raise ValueError("Successor attempt is not in the checked phase")
    if channel is not None and updater._resolve_channel(channel) != plan["channel"]:
        raise ValueError("Stage channel differs from the digest-bound successor plan")
    candidates = _exact_successor_candidates(
        updater,
        selector,
        parent,
        plan["channel"],
        tuple(core.CORE_COMPONENT_IDS),
        core,
    )
    base_plan = _base_plan(updater, plan)
    if (
        base_plan["planId"] != plan["basePlanId"]
        or base_plan["planDigest"] != plan["basePlanDigest"]
    ):
        raise ValueError("Exact selected candidates differ from the successor's base plan binding")
    staged_root = updater._private_state_directory("staged")
    plan_root = staged_root / plan["basePlanId"]
    if plan_root.is_symlink():
        raise ValueError("Refusing symlinked successor base stage directory")
    if plan_root.exists():
        import shutil

        shutil.rmtree(plan_root)
    plan_root.mkdir(parents=True, mode=0o700)
    updater._atomic_json_file(
        updater._private_state_directory("plans") / f"{plan['basePlanId']}.json",
        base_plan,
        mode=0o600,
    )
    staged_components: list[dict[str, Any]] = []
    try:
        for component_id in tuple(core.CORE_COMPONENT_IDS):
            staged_components.append(
                updater._stage_candidate(
                    candidates[component_id], plan_root, plan["basePlanId"], plan["basePlanDigest"]
                )
            )
        updater._revalidate_plan_placement(base_plan, operation="stage")
        updater._atomic_json_file(
            plan_root / "stage.json",
            {
                "schemaVersion": 2,
                "plan": base_plan,
                "channel": plan["channel"],
                "components": staged_components,
                "phase": "staged",
                "createdAt": int(time.time()),
            },
            mode=0o600,
        )
    except Exception:
        import shutil

        shutil.rmtree(plan_root, ignore_errors=True)
        raise
    base_path = plan_root / "stage.json"
    base_record = core._read_private_json(base_path)
    if base_record is None or base_record.get("phase") != "staged":
        raise ValueError("Verified successor Core cohort was not completely staged")
    digests = {
        item.get("componentId"): item.get("artifactDigest")
        for item in base_record.get("components", [])
        if isinstance(item, dict)
    }
    if digests != plan["componentArtifactDigests"]:
        raise ValueError("Staged successor artifact digests differ from the immutable plan")
    core._validate_staged_cohort(updater, base_record.get("components", []))
    staged_plan = {**plan, "phase": "staged"}
    staged_record = {
        "schemaVersion": 1,
        "mode": core.CORE_BOOTSTRAP_MODE,
        "plan": staged_plan,
        "basePlanId": plan["basePlanId"],
        "basePlanDigest": plan["basePlanDigest"],
        "phase": "staged",
        "successorBinding": plan["successorBinding"],
    }
    core._write_private_json(
        updater, Path(updater.state_root) / "staged" / plan_id / "stage.json", staged_record
    )
    staged_attempt = {
        **attempt,
        "phase": "staged",
        "stagedComponentArtifactDigests": digests,
    }
    _write_attempt(updater, core, core._journal_path(updater), transaction, staged_attempt)
    return {
        "status": "staged",
        "mode": core.CORE_BOOTSTRAP_MODE,
        "plan": staged_plan,
        "plans": [staged_plan],
        "components": base_record.get("components", []),
    }


def handle(updater: Any, request: dict[str, Any], core: Any) -> dict[str, Any] | None:
    """Dispatch explicit held-successor requests; ordinary first-Core remains unchanged."""

    if "heldRecovery" not in request:
        return None
    operation = request.get("operation")
    if operation == "check":
        if request.get("includeProducts", False) is not False:
            raise ValueError("Held successor check is Core-only")
        return check(updater, request["heldRecovery"], channel=request.get("channel"), core=core)
    if operation == "stage":
        return stage(
            updater,
            request.get("planId"),
            request.get("planDigest"),
            request["heldRecovery"],
            channel=request.get("channel"),
            core=core,
        )
    if operation == "apply":
        return core.apply(
            updater,
            request.get("planId"),
            request.get("planDigest"),
            request.get("confirmation"),
            channel=request.get("channel"),
            held_recovery=request["heldRecovery"],
        )
    raise ValueError("heldRecovery is supported only for first-Core check, stage, and apply")
