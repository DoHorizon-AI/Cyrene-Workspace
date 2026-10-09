"""Deterministic contract tests for the first Product cohort helper."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_first_products_test", ROOT / "packaging" / "native_first_products.py"
)
assert SPEC is not None and SPEC.loader is not None
first = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = first
SPEC.loader.exec_module(first)


def _receipt() -> dict[str, Any]:
    installer = {
        "debSha256": "sha256:" + "a" * 64,
        "sourceCommit": "b" * 40,
        "targetId": "linux-ubuntu-24.04-x86_64-systemd",
    }
    products = []
    for index, service in enumerate(first.PRODUCTS):
        digit = format(index + 10, "x")
        products.append(
            {
                "service": service,
                "componentId": first.COMPONENT_IDS[service],
                "version": "sha256-" + service,
                "manifestDigest": "sha256:" + (digit * 64),
                "artifactDigest": "sha256:" + (format(index + 1, "x") * 64),
                "bundlePath": (f"/usr/share/cyrene/service-artifacts/{service}/sha256-{service}"),
            }
        )
    material = {
        "schemaVersion": 1,
        "kind": "first-product-cohort",
        "installer": installer,
        "products": products,
    }
    return material | {"receiptDigest": first._digest(first._canonical(material))}


def _write_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: dict[str, Any]) -> Path:
    parent = tmp_path / "native-initialization"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    path = parent / "first-product-cohort.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(first, "RECEIPT_PATH", path)
    monkeypatch.setattr(first, "RECEIPT_PARENT", parent)
    monkeypatch.setattr(first, "ROOT_UID", os.getuid())
    return path


def test_receipt_requires_exact_canonical_five_product_cohort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write_receipt(monkeypatch, tmp_path, _receipt())

    parsed = first._read_receipt(path)

    assert [item["service"] for item in parsed["products"]] == list(first.PRODUCTS)
    assert parsed["receiptDigest"] == first._digest(
        first._canonical({key: value for key, value in parsed.items() if key != "receiptDigest"})
    )


def test_receipt_digest_and_fixed_bundle_path_tampering_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = _receipt()
    receipt["products"][2]["bundlePath"] = "/tmp/attacker/reactor"
    path = _write_receipt(monkeypatch, tmp_path, receipt)

    with pytest.raises(ValueError, match="fixed DEB tree"):
        first._read_receipt(path)


def test_native_core_target_maps_to_exact_product_python_profile() -> None:
    assert (
        first._python_profile_for_core_target("linux-ubuntu-24.04-x86_64-systemd")
        == "linux-ubuntu-24.04-x86_64-python-3.12"
    )
    with pytest.raises(ValueError, match="unsupported native target"):
        first._python_profile_for_core_target("linux-ubuntu-24.04-x86_64-python-3.12")


def test_unit_path_delegates_to_the_deb_managed_contract_verifier(tmp_path: Path) -> None:
    unit_path = tmp_path / "cyrene-catalyst.service"
    unit_bytes = b"verified DEB-managed unit"
    observed: dict[str, Any] = {}

    def validate_unit(
        component: dict[str, Any], candidate: dict[str, Any], *, verify_fragment: bool
    ) -> tuple[bytes, Path, str]:
        observed.update(
            component=component,
            candidate=candidate,
            verify_fragment=verify_fragment,
        )
        return unit_bytes, unit_path, "sha256:" + "a" * 64

    updater = SimpleNamespace(_workload_deb_managed_unit_bytes=validate_unit)
    component = {
        "componentId": "cyrene-catalyst",
        "pythonBundleService": "catalyst",
    }

    path, payload = first._unit_path(
        updater,
        component,
        "catalyst",
        target_profile="linux-ubuntu-24.04-x86_64-python-3.12",
    )

    assert path == unit_path
    assert payload == unit_bytes
    assert observed == {
        "component": component,
        "candidate": {"targetId": "linux-ubuntu-24.04-x86_64-python-3.12"},
        "verify_fragment": True,
    }


def test_unit_path_fails_closed_without_native_contract_verifier() -> None:
    with pytest.raises(TypeError, match="no DEB-managed Product unit verifier"):
        first._unit_path(
            SimpleNamespace(),
            {"pythonBundleService": "catalyst"},
            "catalyst",
            target_profile="linux-ubuntu-24.04-x86_64-python-3.12",
        )


@pytest.mark.parametrize("raw_digest", ["a" * 64, "0" * 64, "f" * 64])
def test_bundle_raw_artifact_digest_maps_to_typed_receipt_digest(raw_digest: str) -> None:
    assert first._typed_bundle_artifact_digest(raw_digest) == f"sha256:{raw_digest}"


@pytest.mark.parametrize(
    "invalid_digest",
    [None, "sha256:" + "a" * 64, "A" * 64, "g" * 64, "a" * 63],
)
def test_bundle_artifact_digest_rejects_noncanonical_representation(
    invalid_digest: Any,
) -> None:
    assert first._typed_bundle_artifact_digest(invalid_digest) is None


class _Updater:
    def __init__(self, tmp_path: Path) -> None:
        self.state_root = tmp_path
        self.install_root = tmp_path / "install"
        self.install_root.mkdir()
        self.components = {
            component_id: {
                "systemdUnit": f"{component_id}.service",
                "restart": {"unit": f"{component_id}.service"},
            }
            for component_id in first.COMPONENT_IDS.values()
        }
        self.started: list[str] = []
        self.persisted: list[dict[str, Any]] = []

    def runner(self, _argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="0\n", stderr="")

    def _atomic_json_file(self, _path: Path, value: dict[str, Any], *, mode: int) -> None:
        assert mode == 0o600
        self.persisted.append(json.loads(json.dumps(value)))

    def _run_systemctl(self, action: str, unit: str) -> None:
        self.started.append(f"{action}:{unit}")

    def _wait_unit_active(self, unit: str) -> None:
        self.started.append(f"active:{unit}")

    def _readiness_for(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "MAINTENANCE_ACTIVE"}


def _plan_and_transaction() -> tuple[dict[str, Any], dict[str, Any]]:
    identities = []
    for service in first.PRODUCTS:
        identities.append(
            {
                "service": service,
                "componentId": first.COMPONENT_IDS[service],
                "version": "v1.0.0",
                "manifestDigest": "sha256:" + "1" * 64,
                "artifactDigest": "sha256:" + "2" * 64,
                "sourceCommit": "3" * 40,
                "targetProfileId": "linux-ubuntu-24.04-x86_64-python-3.12",
                "bundlePath": f"/usr/share/cyrene/service-artifacts/{service}/v1.0.0",
            }
        )
    block = {
        "schemaVersion": 1,
        "receiptDigest": "sha256:" + "4" * 64,
        "installer": {
            "debSha256": "sha256:" + "5" * 64,
            "sourceCommit": "6" * 40,
            "targetId": "linux-ubuntu-24.04-x86_64-systemd",
        },
        "products": identities,
    }
    plan = {
        "planId": "plan-" + "a" * 32,
        "planDigest": "sha256:" + "a" * 64,
        "targetId": block["installer"]["targetId"],
        "catalogGeneration": 4,
        "activitySources": [f"cyrene-{name}" for name in first.PRODUCTS],
        "firstProducts": block,
    }
    core_components = [
        {
            "componentId": component_id,
            "version": "1.0.0",
            "manifestDigest": "sha256:" + "b" * 64,
        }
        for component_id in (
            "cyrene-linux-sys-adapter",
            "cyrene-nvidia-adapter",
            "cyrene-sandboxd",
            "cyrene-kernel",
        )
    ]
    transaction = {
        "phase": "starting",
        "maintenanceToken": "token-that-is-long-enough-for-a-fixture",
        "components": core_components,
        "firstProducts": {
            "schemaVersion": 1,
            "receiptDigest": block["receiptDigest"],
            "products": identities,
            "phase": "staged",
            "ownedPointers": [],
            "ownedUnits": [],
        },
    }
    return plan, transaction


def test_products_cannot_start_before_core_hold_and_live_core(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = _Updater(tmp_path)
    plan, transaction = _plan_and_transaction()
    transaction.pop("maintenanceToken")

    monkeypatch.setattr(first, "_plan_products", lambda *_args: [])

    with pytest.raises(PermissionError, match="durable Core maintenance hold"):
        first.activate_held(updater, plan, transaction)
    assert updater.started == []


def test_activation_targets_only_the_exact_five_plan_products(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = _Updater(tmp_path)
    plan, transaction = _plan_and_transaction()
    actual = [
        dict(row, manifest={"health": {"path": "/health"}})
        for row in plan["firstProducts"]["products"]
    ]
    activations: list[str] = []
    monkeypatch.setattr(first, "_plan_products", lambda *_args: actual)
    monkeypatch.setattr(first, "_check_fresh", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(first, "_assert_core_started", lambda *_args: None)
    monkeypatch.setattr(
        first,
        "_assert_zero_runtime",
        lambda *_args, **_kwargs: {"status": "MAINTENANCE_ACTIVE"},
    )
    monkeypatch.setattr(
        first,
        "_write_unit_and_pointer",
        lambda _u, product, record: (
            activations.append(product["service"]),
            record.setdefault("ownedPointers", []).append(product["service"]),
        ),
    )
    monkeypatch.setattr(
        first, "_live_product", lambda _u, product: first.PRODUCTS.index(product["service"]) + 100
    )
    monkeypatch.setattr(first, "_persist_transaction", lambda *_args: None)
    monkeypatch.setattr(first, "_main_pid", lambda *_args: 0)
    monkeypatch.setattr(first, "_probe_api", lambda *_args: None)
    monkeypatch.setattr(first, "_bundle_module", lambda _u: SimpleNamespace())

    result = first.activate_held(updater, plan, transaction)

    assert result["status"] == "active"
    assert activations == list(first.PRODUCTS)
    starts = [item for item in updater.started if item.startswith("start:")]
    assert len(starts) == len(first.PRODUCTS)
    assert all(f"start:cyrene-{service}.service" in starts for service in first.PRODUCTS)


def test_broker_unknown_activity_is_not_product_readiness() -> None:
    updater = SimpleNamespace(
        _activity_catalog=lambda: ({"generation": 4}, ["cyrene-yield"]),
        _readiness_for=lambda *_args, **_kwargs: {
            "status": "UNKNOWN",
            "unknown_activity_sources": ["cyrene-yield"],
            "active_tasks": [],
            "active_task_count": 0,
            "inflight_runtime_admission_count": 0,
            "active_worker_count": 0,
            "active_allocation_count": 0,
        },
    )
    plan = {"catalogGeneration": 4, "activitySources": ["cyrene-yield"]}

    with pytest.raises(RuntimeError, match="strict READY"):
        first._assert_zero_runtime(updater, plan, allow_maintenance=False)


def test_busy_post_end_phase_defers_without_runtime_prepare_or_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = _Updater(tmp_path)
    plan, transaction = _plan_and_transaction()
    transaction["gateReleaseConfirmed"] = True
    monkeypatch.setattr(first, "_plan_products", lambda *_args: plan["firstProducts"]["products"])
    monkeypatch.setattr(
        first,
        "_assert_zero_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("busy")),
    )
    monkeypatch.setattr(first, "_persist_transaction", lambda *_args: None)

    result = first.complete_post_end(updater, plan, transaction)

    assert result["status"] == "pending"
    assert transaction["firstProducts"]["phase"] == "post_end_pending"
    assert updater.started == []


def test_signed_observer_preserves_serving_projection_when_broker_has_active_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = _Updater(tmp_path)
    plan, transaction = _plan_and_transaction()
    record = transaction["firstProducts"]
    manifest = tmp_path / "platform.json"
    monkeypatch.setattr(first, "PLATFORM_RUNTIME_MANIFEST", manifest)
    monkeypatch.setattr(first, "_runtime_identity_units", lambda *_args: {"unit": {}})
    monkeypatch.setattr(
        first, "_active_runtime_helper", lambda *_args: tmp_path / "signed-observer.py"
    )
    monkeypatch.setattr(first, "_root_file_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(first, "_verify_platform_projection", lambda: None)
    monkeypatch.setattr(first, "_persist_transaction", lambda *_args: None)
    observed: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        observed.append(argv)
        manifest.write_text('{"profile":"serving"}', encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "READY",
                    "authorityStatus": "ACTIVE_TASKS",
                    "activeWorkers": 1,
                    "activeAllocations": 0,
                }
            ),
            stderr="",
        )

    updater.runner = run
    strict_checks: list[bool] = []
    monkeypatch.setattr(
        first,
        "_assert_zero_runtime",
        lambda *_args, **kwargs: strict_checks.append(kwargs["allow_maintenance"]),
    )

    with pytest.raises(RuntimeError, match="activity remains active"):
        first._observe_platform_runtime(updater, plan, transaction, record)

    assert len(observed) == 1
    assert observed[0][-2:] == [
        "--identity-json",
        str(tmp_path / "native-first-bootstrap" / "managed-runtime-identity.json"),
    ]
    assert manifest.read_text(encoding="utf-8") == '{"profile":"serving"}'
    assert record["observeAuthorityStatus"] == "ACTIVE_TASKS"
    assert record["platformManifestDigest"] == first._digest(manifest.read_bytes())
    assert strict_checks == []


def test_signed_observer_requires_fresh_zero_after_ready_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = _Updater(tmp_path)
    plan, transaction = _plan_and_transaction()
    record = transaction["firstProducts"]
    manifest = tmp_path / "platform.json"
    monkeypatch.setattr(first, "PLATFORM_RUNTIME_MANIFEST", manifest)
    monkeypatch.setattr(first, "_runtime_identity_units", lambda *_args: {"unit": {}})
    monkeypatch.setattr(
        first, "_active_runtime_helper", lambda *_args: tmp_path / "signed-observer.py"
    )
    monkeypatch.setattr(first, "_root_file_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(first, "_verify_platform_projection", lambda: None)
    monkeypatch.setattr(first, "_persist_transaction", lambda *_args: None)
    updater.runner = lambda _argv, **_kwargs: (
        manifest.write_text('{"profile":"serving"}', encoding="utf-8"),
        SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "READY",
                    "authorityStatus": "READY",
                    "activeWorkers": 0,
                    "activeAllocations": 0,
                }
            ),
            stderr="",
        ),
    )[1]
    strict_checks: list[bool] = []
    monkeypatch.setattr(
        first,
        "_assert_zero_runtime",
        lambda *_args, **kwargs: strict_checks.append(kwargs["allow_maintenance"]),
    )

    first._observe_platform_runtime(updater, plan, transaction, record)

    assert strict_checks == [False]
    assert record["observeAuthorityStatus"] == "READY"
    assert record["observeActiveWorkers"] == 0


def test_observer_path_is_resolved_from_active_signed_broker_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    updater = _Updater(tmp_path)
    pointer = "v1.0.0--" + "a" * 64
    helper = (
        updater.install_root
        / "components"
        / "cyrene-runtime-maintenance"
        / "releases"
        / pointer
        / first.MANAGED_RUNTIME_HELPER
    )
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"signed observer fixture")
    helper.chmod(0o644)
    monkeypatch.setattr(first, "ROOT_UID", os.getuid())
    monkeypatch.setattr(
        updater, "_active_native_pointer_identity", lambda _component: pointer, raising=False
    )
    monkeypatch.setattr(
        updater,
        "_read_active_receipt",
        lambda _component: {
            "componentId": "cyrene-runtime-maintenance",
            "version": "v1.0.0",
            "manifestDigest": "sha256:" + "a" * 64,
            "releaseIdentity": "sha256:" + "a" * 64,
            "manifest": {
                "artifact": {
                    "files": {
                        first.MANAGED_RUNTIME_HELPER.as_posix(): first._digest(helper.read_bytes())
                    }
                }
            },
        },
        raising=False,
    )

    assert first._active_runtime_helper(updater) == helper

    monkeypatch.setattr(
        updater,
        "_read_active_receipt",
        lambda _component: {
            "componentId": "cyrene-runtime-maintenance",
            "version": "v1.0.0",
            "manifestDigest": "sha256:" + "a" * 64,
            "releaseIdentity": "sha256:" + "a" * 64,
            "manifest": {"artifact": {"files": {}}},
        },
        raising=False,
    )
    with pytest.raises(RuntimeError, match="omits the managed-runtime observer"):
        first._active_runtime_helper(updater)
