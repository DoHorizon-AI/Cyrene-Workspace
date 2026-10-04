"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: tests.test_native_components_v2_acceptance                   │
│ Role: Guard real-host acceptance evidence and fail-closed recording.  │
│                                                                      │
│ 模块职责：验证真机证据记录与失败关闭行为。                              │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = (
    WORKSPACE_ROOT / "tooling" / "acceptance" / "native-components-v2" / "native_acceptance.py"
)
SPEC = importlib.util.spec_from_file_location("native_acceptance_test_module", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
native_acceptance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = native_acceptance
SPEC.loader.exec_module(native_acceptance)
sys.modules["native_acceptance"] = native_acceptance

ADMIN_PATH = RUNNER_PATH.with_name("admin_initialize.py")
ADMIN_SPEC = importlib.util.spec_from_file_location(
    "native_admin_initialize_test_module", ADMIN_PATH
)
assert ADMIN_SPEC is not None and ADMIN_SPEC.loader is not None
admin_initialize = importlib.util.module_from_spec(ADMIN_SPEC)
sys.modules[ADMIN_SPEC.name] = admin_initialize
ADMIN_SPEC.loader.exec_module(admin_initialize)


def _new_run(tmp_path: Path) -> Path:
    """Create a run ledger for the current source checkout.

    中文：为当前源码检出创建验收台账。
    """

    return native_acceptance.create_run(
        tmp_path / "run",
        native_acceptance.DEFAULT_SSH_ALIAS,
        [f"Workspace={WORKSPACE_ROOT}"],
    )


def test_new_run_keeps_host_admission_unknown_and_all_phases_unrun(tmp_path: Path) -> None:
    """A local source snapshot must not infer host or workload acceptance.

    中文：本地源码快照不得推断主机或工作负载已验收。
    """

    run_path = _new_run(tmp_path)
    value = native_acceptance.load_run(run_path)

    assert value["sources"]["Workspace"]["commit"]
    assert value["taskAdmission"]["status"] == "UNKNOWN"
    assert value["taskAdmission"]["applyAdmission"] == "CLOSED"
    assert value["resources"]["gpuIdleConclusion"] == "UNKNOWN"
    assert all(
        mode["status"] == "NOT_RUN" for phase in value["phases"].values() for mode in phase.values()
    )
    assert run_path.stat().st_mode & 0o077 == 0


def test_latest_source_readback_preserves_probe_build_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer integration head is recorded separately from the probe's source.

    中文：更新后的集成 HEAD 单独记录，不覆盖只读查询器的构建来源。
    """

    run_path = _new_run(tmp_path)
    original_commit = native_acceptance.load_run(run_path)["sources"]["Workspace"]["commit"]

    def current_source(name: str, source_path: Path) -> dict[str, object]:
        return {
            "path": str(source_path),
            "branch": "codex/test",
            "commit": "f" * 40,
            "workingTree": "CLEAN",
            "dirtyPaths": [],
        }

    monkeypatch.setattr(native_acceptance, "source_identity", current_source)
    snapshot = native_acceptance.refresh_source_heads(run_path)
    ledger = native_acceptance.load_run(run_path)

    assert snapshot["items"]["Workspace"]["commit"] == "f" * 40
    assert ledger["sources"]["Workspace"]["commit"] == original_commit
    assert ledger["latestSourceHeads"]["items"]["Workspace"]["commit"] == "f" * 40
    assert "Latest local integration heads" in native_acceptance.render_report(run_path)


def test_latest_source_readback_can_add_current_checkout_without_rewriting_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additional repositories belong only to the latest integration snapshot.

    中文：追加仓库只进入最新集成快照，不改写历史源码集合。
    """

    run_path = _new_run(tmp_path)
    yield_path = tmp_path / "Cyrene-Yield"
    yield_path.mkdir()

    def current_source(name: str, source_path: Path) -> dict[str, object]:
        return {
            "path": str(source_path),
            "branch": "codex/test",
            "commit": "e" * 40,
            "workingTree": "DIRTY",
            "dirtyPaths": ["src/cyrene_yield/worker.py"],
        }

    monkeypatch.setattr(native_acceptance, "source_identity", current_source)
    snapshot = native_acceptance.refresh_source_heads(run_path, [f"Yield={yield_path}"])
    ledger = native_acceptance.load_run(run_path)

    assert "Yield" not in ledger["sources"]
    assert snapshot["items"]["Yield"]["commit"] == "e" * 40
    assert ledger["latestSourceHeads"]["items"]["Yield"]["workingTree"] == "DIRTY"


def test_simulated_evidence_cannot_mark_phase_pass(tmp_path: Path) -> None:
    """A fixture receipt cannot be promoted into a real-host PASS.

    中文：fixture 回执不能升级成真机 PASS。
    """

    run_path = _new_run(tmp_path)
    evidence = tmp_path / "fixture.log"
    evidence.write_text("simulated workload\n", encoding="utf-8")

    with pytest.raises(native_acceptance.AcceptanceError, match="cannot mark a phase PASS"):
        native_acceptance.record_phase(
            run_path,
            "yield_llama_factory_one_step",
            "cold",
            "PASS",
            1200,
            evidence,
            "SIMULATED",
            "",
        )

    value = native_acceptance.load_run(run_path)
    assert value["phases"]["yield_llama_factory_one_step"]["cold"]["status"] == "NOT_RUN"


def test_phase_record_requires_measured_evidence_and_preserves_warm_not_run(
    tmp_path: Path,
) -> None:
    """A measured phase record is digest-bound and does not fill the other temperature.

    中文：阶段耗时记录绑定证据摘要，且不会自动填充另一种运行温度。
    """

    run_path = _new_run(tmp_path)
    evidence = tmp_path / "blocker.txt"
    evidence.write_text("authoritative result: blocker present\n", encoding="utf-8")

    with pytest.raises(native_acceptance.AcceptanceError, match="requires a measured duration"):
        native_acceptance.record_phase(
            run_path,
            "active_task_apply_refused",
            "cold",
            "FAIL",
            None,
            evidence,
            "REAL_HOST",
            "",
        )

    native_acceptance.record_phase(
        run_path,
        "active_task_apply_refused",
        "cold",
        "BLOCKED",
        None,
        None,
        None,
        "Authoritative activity sources are not initialized.",
    )
    value = native_acceptance.load_run(run_path)
    cold = value["phases"]["active_task_apply_refused"]["cold"]
    warm = value["phases"]["active_task_apply_refused"]["warm"]
    assert cold["status"] == "BLOCKED"
    assert warm["status"] == "NOT_RUN"
    assert "Authoritative activity sources are not initialized" in native_acceptance.render_report(
        run_path
    )


def test_read_only_inventory_cannot_claim_worker_lease_or_idle(tmp_path: Path) -> None:
    """Telemetry-only inventory remains UNKNOWN and apply stays closed.

    中文：仅有遥测的清单仍为 UNKNOWN，apply 准入保持关闭。
    """

    run_path = _new_run(tmp_path)
    inventory = {
        "taskLeaseWorkerState": "UNKNOWN",
        "applyAdmission": "CLOSED",
        "gpuIdleConclusion": "UNKNOWN",
        "gpuTelemetry": {"stdout": "NVIDIA GeForce RTX 3090, 550.90.07, 24576 MiB, 15 MiB, 0 %"},
        "existingManualRuntimeProcesses": {
            "9024": {
                "present": True,
                "executable": "/home/operator/runtime/bin/cyrene-kernel",
            }
        },
        "managedSystemUnits": {"cyrene-kernel.service": {"stdout": "LoadState=not-found"}},
        "dockerSocketPresent": True,
        "dockerSocketAccessible": False,
    }
    native_acceptance.attach_remote_result(run_path, inventory)

    value = native_acceptance.load_run(run_path)
    assert value["target"]["inventoryStatus"] == "OBSERVED_READ_ONLY"
    assert value["taskAdmission"]["status"] == "UNKNOWN"
    assert value["taskAdmission"]["applyAdmission"] == "CLOSED"
    assert value["resources"]["gpuIdleConclusion"] == "UNKNOWN"
    report = native_acceptance.render_report(run_path)
    assert "UNKNOWN" in report
    assert "cyrene-kernel" in report
    assert "24576 MiB total / 15 MiB used" in report
    assert "NVIDIA driver version (read-only `nvidia-smi`): `550.90.07`" in report
    assert "GPU-test-id" not in report


def test_inventory_uses_broker_unit_state_without_global_binary_assumption() -> None:
    """The broker is resolved through a verified active component release.

    中文：维护代理通过已验证活动组件发行解析，不假设存在全局可执行文件。
    """

    assert '"brokerExecutablePresent"' not in native_acceptance.REMOTE_PROBE_SCRIPT
    assert "/usr/bin/cyrene-runtime-maintenance" not in native_acceptance.REMOTE_PROBE_SCRIPT
    assert '"brokerUnitState"' in native_acceptance.REMOTE_PROBE_SCRIPT


def test_bootstrap_plan_requires_exact_helper_status_and_all_digest_identities() -> None:
    """Only the helper's exact read-only confirmation contract is accepted.

    中文：仅接受 helper 原样返回的只读确认合同和完整摘要 tuple。
    """

    plan = {
        "status": "confirmation_required",
        "planDigest": "sha256:" + "a" * 64,
        "componentId": "cyrene-runtime-maintenance",
        "targetId": "linux-ubuntu-22.04-x86_64-systemd",
        "version": "1.2.3",
        "manifestDigest": "sha256:" + "b" * 64,
        "artifactDigest": "sha256:" + "c" * 64,
        "indexDigest": "sha256:" + "d" * 64,
    }
    assert (
        admin_initialize._verify_bootstrap_plan(plan, "linux-ubuntu-22.04-x86_64-systemd")
        == plan["planDigest"]
    )
    with pytest.raises(admin_initialize.AdminInitializationError, match="canonical exact plan"):
        admin_initialize._verify_bootstrap_plan(
            {**plan, "status": "confirmation-required"},
            "linux-ubuntu-22.04-x86_64-systemd",
        )


def test_compiled_target_loader_uses_real_updater_and_generation_nine_catalog() -> None:
    """Load the checked-in updater exactly as initialization does.

    中文：用真实 updater 与第九代可信 catalog 覆盖首次安装的动态加载路径。
    """

    helper = WORKSPACE_ROOT / "packaging" / "component_updates.py"
    catalog = WORKSPACE_ROOT / "governance" / "component-catalog-v1.json"
    before = list(sys.path)

    target_id = admin_initialize._load_compiled_target_id(helper, catalog)

    assert target_id in {
        "linux-ubuntu-22.04-x86_64-systemd",
        "linux-ubuntu-24.04-x86_64-systemd",
    }
    assert sys.path == before
    assert "cyrene_admin_component_updates" not in sys.modules


def test_fresh_activity_catalog_gate_preserves_any_existing_state(tmp_path: Path) -> None:
    """Fresh initialization refuses existing catalog, token, or runtime data.

    中文：已有 catalog、token 或 runtime 数据时拒绝 fresh-only 初始化。
    """

    admin_initialize._fresh_activity_state(tmp_path)
    runtime = tmp_path / "var/lib/cyrene/runtime"
    runtime.mkdir(parents=True)
    admin_initialize._fresh_activity_state(tmp_path)
    (runtime / "existing-journal.json").write_text("{}", encoding="utf-8")
    with pytest.raises(admin_initialize.AdminInitializationError, match="Existing runtime data"):
        admin_initialize._fresh_activity_state(tmp_path)
    (runtime / "existing-journal.json").unlink()
    catalog = runtime / "activity-sources.json"
    catalog.write_text("{}", encoding="utf-8")
    with pytest.raises(admin_initialize.AdminInitializationError, match="trusted state"):
        admin_initialize._fresh_activity_state(tmp_path)


def test_activity_sources_come_from_exact_signed_and_installed_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner IDs and credential files are parsed from matching Product units.

    中文：owner ID 与 credential 路径必须来自相同的签名和已安装 unit 字节。
    """

    extracted = tmp_path / "deb/usr/lib/systemd/system"
    installed = tmp_path / "installed"
    extracted.mkdir(parents=True)
    installed.mkdir()
    for source_id in sorted(admin_initialize.PRODUCT_SOURCE_IDS):
        unit = f"cyrene-{source_id.removeprefix('cyrene-')}.service"
        payload = (
            "[Service]\n"
            "User=cyrene\n"
            "Group=cyrene\n"
            "EnvironmentFile=/etc/cyrene/runtime-activity-sources.env\n"
            "LoadCredential=activity-token:/etc/cyrene/runtime-activity-source-tokens/"
            f"{source_id}.token\n"
        ).encode()
        (extracted / unit).write_bytes(payload)
        (installed / unit).write_bytes(payload)

    monkeypatch.setattr(
        admin_initialize.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=1001 if name == "cyrene" else 0),
    )
    monkeypatch.setattr(
        admin_initialize.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid=2001 if name == "cyrene" else 3001),
    )
    arguments, evidence = admin_initialize._activity_source_arguments(extracted, installed)
    assert arguments == [
        item
        for source_id in sorted(admin_initialize.PRODUCT_SOURCE_IDS)
        for item in ("--source", f"{source_id}=1001:2001")
    ]
    assert evidence["catalogGid"] == 3001
    assert set(evidence["sources"]) == admin_initialize.PRODUCT_SOURCE_IDS
    (installed / "cyrene-yield.service").write_bytes(b"different unit bytes")
    with pytest.raises(
        admin_initialize.AdminInitializationError, match="differs from verified DEB"
    ):
        admin_initialize._activity_source_arguments(extracted, installed)


def test_managed_runtime_helper_must_be_inside_activated_signed_release(tmp_path: Path) -> None:
    """The archive helper path and bytes are checked against the active manifest.

    中文：受管 runtime helper 路径与字节必须匹配已激活 manifest。
    """

    install_root = tmp_path / "usr/lib/cyrene"
    release = install_root / "components/cyrene-runtime-maintenance/releases/1.2.3--abcd"
    helper = release / admin_initialize.BOOTSTRAP_HELPER_RELATIVE
    helper.parent.mkdir(parents=True)
    helper_bytes = b"verified managed runtime helper"
    helper.write_bytes(helper_bytes)
    helper_digest = "sha256:" + hashlib.sha256(helper_bytes).hexdigest()
    manifest = {
        "manifestDigest": "sha256:" + "a" * 64,
        "artifact": {
            "sha256": "sha256:" + "b" * 64,
            "files": {admin_initialize.BOOTSTRAP_HELPER_RELATIVE.as_posix(): helper_digest},
        },
    }
    (release / "component-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    activation = {
        "releasePath": str(release),
        "manifestDigest": manifest["manifestDigest"],
        "artifactDigest": manifest["artifact"]["sha256"],
    }
    resolved, loaded_manifest = admin_initialize._verified_broker_helper(
        activation, install_root=install_root
    )
    assert resolved == helper
    assert loaded_manifest == manifest


def test_admin_interface_needs_exact_source_pins_and_explicit_broker_start() -> None:
    """The operator start is opt-in and source pins have no defaults.

    中文：broker 启动需显式选择，源码 pin 不设默认值。
    """

    parser = admin_initialize.build_parser()
    arguments = parser.parse_args(
        [
            "--release-directory",
            "/immutable/assets",
            "--expected-source-ref",
            "refs/heads/develop",
            "--expected-source-commit",
            "a" * 40,
            "--index",
            "/assets/index.json",
            "--index-attestation",
            "/assets/index.attestation.jsonl",
            "--manifest",
            "/assets/manifest.json",
            "--artifact",
            "/assets/broker.tar.gz",
            "--artifact-attestation",
            "/assets/broker.attestation.jsonl",
            "--channel",
            "preview",
        ]
    )
    assert arguments.start_broker is False
    assert arguments.expected_source_commit == "a" * 40


def test_preparation_receipt_records_only_the_isolated_user_directory(tmp_path: Path) -> None:
    """Preparation receipt records its exact target without claiming installation.

    中文：准备回执记录精确目标目录，但不表示已安装软件。
    """

    run_path = _new_run(tmp_path)
    native_acceptance.attach_remote_result(
        run_path,
        {},
        prepared="CREATED:/home/operator/.local/share/cyrene/native-operator-acceptance/20261004",
    )

    ledger = native_acceptance.load_run(run_path)
    report = native_acceptance.render_report(run_path)
    assert ledger["target"]["preparedUserDirectory"] == "CREATED"
    assert ledger["target"]["preparedPath"].endswith("/20261004")
    assert "Isolated user directory: `CREATED`" in report
    assert "Admin initialization command: `NOT_GENERATED`" in report
    assert "root-owned backup" in report
    assert "sudo" not in report


def test_report_summarizes_digest_bound_observations_without_unlisted_fields(
    tmp_path: Path,
) -> None:
    """The receipt reports key evidence digests while leaving arbitrary fields out.

    中文：回执呈现关键证据摘要，并省略未列入摘要的任意字段。
    """

    run_path = _new_run(tmp_path)
    evidence = tmp_path / "observation.log"
    evidence.write_text("real host observation\n", encoding="utf-8")
    document = tmp_path / "observation.json"
    artifact_digest = "sha256:" + "a" * 64
    document.write_text(
        json.dumps(
            {
                "componentId": "cyrene-yield",
                "sourceCommit": "a" * 40,
                "artifactDigest": artifact_digest,
                "credentialFilePath": "/etc/cyrene/private.token",
            }
        ),
        encoding="utf-8",
    )
    native_acceptance.record_observation(
        run_path, "artifact", document, evidence, "RELEASE_PROVENANCE"
    )
    document.write_text(
        json.dumps(
            {
                "planId": "plan-123",
                "planDigest": "sha256:" + "b" * 64,
                "components": [{"componentId": "cyrene-yield", "artifactDigest": artifact_digest}],
            }
        ),
        encoding="utf-8",
    )
    native_acceptance.record_observation(run_path, "plan", document, evidence, "REAL_HOST")
    document.write_text(
        json.dumps(
            {
                "taskId": "task-456",
                "engine": "training.llama-factory.v1",
                "status": "succeeded",
                "completedSteps": 1,
                "workerId": "worker-1",
                "leaseId": "lease-1",
            }
        ),
        encoding="utf-8",
    )
    native_acceptance.record_observation(run_path, "task", document, evidence, "SIMULATED")
    document.write_text(
        json.dumps(
            {
                "resourceId": "gpu-resource-1",
                "kernelBindingProofSha256": "sha256:" + "c" * 64,
                "gpuIdleConclusion": "UNKNOWN",
            }
        ),
        encoding="utf-8",
    )
    native_acceptance.record_observation(run_path, "resource", document, evidence, "SIMULATED")

    report = native_acceptance.render_report(run_path)
    assert "Artifact observations" in report
    assert "Plan observations" in report
    assert "planId=`plan-123`" in report
    assert "taskId=`task-456`" in report
    assert "workerId=`worker-1`" in report
    assert "kernelBindingProofSha256=`sha256:" + "c" * 64 in report
    assert "document SHA-256 `sha256:" in report
    assert "sha256:sha256:" not in report
    assert "/etc/cyrene/private.token" not in report
    assert native_acceptance.load_run(run_path)["tasks"]["status"] == "UNKNOWN"


def test_inventory_rejects_claimed_idle_or_open_admission(tmp_path: Path) -> None:
    """Read-only host inventory cannot open a privileged action gate.

    中文：只读主机清单不能打开特权操作准入。
    """

    run_path = _new_run(tmp_path)
    inventory = {"taskLeaseWorkerState": "IDLE", "applyAdmission": "READY"}

    with pytest.raises(native_acceptance.AcceptanceError, match="cannot set task/Lease/Worker"):
        native_acceptance.attach_remote_result(run_path, inventory)


def test_observation_rejects_secret_fields_and_requires_exact_artifact_digest(
    tmp_path: Path,
) -> None:
    """Evidence JSON must have an exact digest and no credential-bearing fields.

    中文：证据 JSON 必须提供精确摘要，并且不得包含凭据字段。
    """

    run_path = _new_run(tmp_path)
    evidence = tmp_path / "real-observation.log"
    evidence.write_text("verified bytes\n", encoding="utf-8")
    document = tmp_path / "artifact.json"

    artifact_document = {
        "componentId": "cyrene-test-component",
        "sourceCommit": "a" * 40,
        "artifactDigest": "sha256:" + "a" * 64,
    }
    document.write_text(json.dumps(artifact_document), encoding="utf-8")
    native_acceptance.record_observation(
        run_path, "artifact", document, evidence, "RELEASE_PROVENANCE"
    )
    value = native_acceptance.load_run(run_path)
    assert value["artifacts"]["items"][0]["document"]["artifactDigest"] == "sha256:" + "a" * 64

    document.write_text(
        json.dumps(
            {
                "componentId": "cyrene-test-component",
                "sourceCommit": "a" * 40,
                "artifactDigest": "sha256:" + "b" * 64,
                "accessToken": "do-not-store",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(native_acceptance.AcceptanceError, match="without credential-like fields"):
        native_acceptance.record_observation(
            run_path, "artifact", document, evidence, "RELEASE_PROVENANCE"
        )


def test_plan_must_bind_previously_recorded_component_artifact_digest(tmp_path: Path) -> None:
    """The plan observation must match a recorded artifact by component and SHA.

    中文：计划观察必须按组件 ID 和 SHA 绑定已有制品记录。
    """

    run_path = _new_run(tmp_path)
    evidence = tmp_path / "provenance.log"
    evidence.write_text("attestation verifier output\n", encoding="utf-8")
    artifact_digest = "sha256:" + "c" * 64
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        json.dumps(
            {
                "componentId": "cyrene-test-component",
                "sourceCommit": "b" * 40,
                "artifactDigest": artifact_digest,
            }
        ),
        encoding="utf-8",
    )
    native_acceptance.record_observation(
        run_path, "artifact", artifact, evidence, "RELEASE_PROVENANCE"
    )

    plan = tmp_path / "plan.json"
    plan_document = {
        "planId": "plan-test-1",
        "planDigest": "sha256:" + "d" * 64,
        "components": [{"componentId": "cyrene-test-component", "artifactDigest": artifact_digest}],
    }
    plan.write_text(json.dumps(plan_document), encoding="utf-8")
    native_acceptance.record_observation(run_path, "plan", plan, evidence, "REAL_HOST")

    plan_document["components"][0]["artifactDigest"] = "sha256:" + "e" * 64
    plan.write_text(json.dumps(plan_document), encoding="utf-8")
    with pytest.raises(native_acceptance.AcceptanceError, match="do not match a recorded artifact"):
        native_acceptance.record_observation(run_path, "plan", plan, evidence, "REAL_HOST")


def test_kernel_gpu_pass_requires_real_binding_proof(tmp_path: Path) -> None:
    """Real host labeling alone cannot pass Kernel GPU binding acceptance.

    中文：仅标注为真机证据不足以通过 Kernel GPU 绑定验收。
    """

    run_path = _new_run(tmp_path)
    evidence = tmp_path / "kernel-preflight.log"
    evidence.write_text("no binding proof\n", encoding="utf-8")

    with pytest.raises(
        native_acceptance.AcceptanceError, match="requires real Kernel binding proof"
    ):
        native_acceptance.record_phase(
            run_path,
            "kernel_gpu_preflight",
            "cold",
            "PASS",
            500,
            evidence,
            "REAL_PRODUCT",
            "",
        )


def test_prepare_script_is_confined_to_the_dedicated_user_directory() -> None:
    """The remote preparation script must not invoke package or service operations.

    中文：远端准备脚本不得执行软件包或服务操作。
    """

    script = native_acceptance.REMOTE_PREPARE_SCRIPT
    assert 'base="$HOME/.local/share/cyrene/native-operator-acceptance"' in script
    assert 'target="$base/20261004"' in script
    assert "sudo" not in script
    assert "dpkg" not in script
    assert "systemctl" not in script
    assert "nvidia-smi" not in script


def test_admin_tuple_template_has_no_invented_signature_or_install_command() -> None:
    """Unverified release and signer identity fields remain empty.

    中文：未核实的发行与签名身份字段必须保持空值。
    """

    template_path = RUNNER_PATH.parent / "admin-init-tuple.template.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))

    def empty(value: object) -> bool:
        if isinstance(value, dict):
            return all(empty(nested) for nested in value.values())
        if isinstance(value, list):
            return all(empty(nested) for nested in value)
        if isinstance(value, bool):
            return value is False
        return value == ""

    assert template["schemaVersion"] == 1
    assert all(empty(value) for key, value in template.items() if key != "schemaVersion")
    runbook = (RUNNER_PATH.parent / "admin-initialization.md").read_text(encoding="utf-8")
    assert "NO INSTALL COMMAND GENERATED" in runbook
    assert "`sudo`, `dpkg`, broker-start" in runbook


def _kernel_probe_result(readiness_status: str = "RESPONDED") -> dict[str, object]:
    """Create a sanitized read-only Kernel receipt fixture.

    中文：创建已净化的 Kernel 只读回执 fixture。
    """

    readiness: dict[str, object] = {"status": readiness_status}
    if readiness_status == "RESPONDED":
        readiness.update(
            {
                "activeTaskCount": 0,
                "activeWorkerCount": 0,
                "activeLeaseOrAllocationCount": 0,
                "inflightRuntimeAdmissionCount": 0,
                "unknownActivitySourceCount": 0,
                "blockerCodes": [],
            }
        )
    return {
        "schemaVersion": 1,
        "capabilities": {"status": "RESPONDED"},
        "readiness": readiness,
        "taskLeaseWorkerState": "UNKNOWN",
        "gpuIdleConclusion": "UNKNOWN",
        "applyAdmissionDecision": "CLOSED",
    }


def test_kernel_readiness_counters_never_open_admission_or_infer_idle() -> None:
    """Even zero read-only counters do not become an idle or apply conclusion.

    中文：只读计数即使为零，也不得推断 GPU 空闲或开放 apply。
    """

    native_acceptance._validate_kernel_probe_result(_kernel_probe_result())
    result = _kernel_probe_result()
    result["gpuIdleConclusion"] = "IDLE"
    with pytest.raises(native_acceptance.AcceptanceError, match="fail-closed contract"):
        native_acceptance._validate_kernel_probe_result(result)


def test_query_kernel_readiness_records_only_allowlisted_readonly_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful synthetic UDS receipt remains UNKNOWN and apply-closed.

    中文：合成 UDS 回执只能保持 UNKNOWN 和 apply closed。
    """

    run_path = _new_run(tmp_path)
    native_acceptance.attach_remote_result(
        run_path,
        {},
        prepared="CREATED:/home/operator/.local/share/cyrene/native-operator-acceptance/20261004",
    )
    binary = tmp_path / "probe"
    binary.write_bytes(b"read-only-client")
    binary.chmod(0o700)
    ledger = native_acceptance.load_run(run_path)
    ledger["kernelReadonlyProbe"]["build"] = {
        "sourceStableDuringBuild": True,
        "binaryPath": str(binary),
        "binarySha256": native_acceptance.sha256_bytes(binary.read_bytes()),
        "sourceSha256": "sha256:" + "a" * 64,
        "rpcAllowlist": [
            "cyrene.core.v1.KernelService/GetKernelCapabilities",
            "cyrene.core.v2.KernelAuthorityService/GetUpdateReadiness",
        ],
    }
    native_acceptance.store_run(run_path, ledger)

    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="READY\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(_kernel_probe_result()),
                stderr="",
            ),
        ]
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr(native_acceptance.subprocess, "run", fake_run)
    recorded = native_acceptance.query_kernel_readiness(
        run_path,
        alias=native_acceptance.DEFAULT_SSH_ALIAS,
        socket_path="/home/operator/runtime/run/kernel.sock",
    )
    final = native_acceptance.load_run(run_path)

    assert recorded["taskLeaseWorkerState"] == "UNKNOWN"
    assert recorded["gpuIdleConclusion"] == "UNKNOWN"
    assert recorded["applyAdmissionDecision"] == "CLOSED"
    assert commands[0][0] == "ssh"
    assert commands[1][0] == "scp"
    assert commands[2][0] == "ssh"
    assert all("StrictHostKeyChecking=yes" in command for command in commands)
    assert "SetKernel" not in str(recorded["rpcAllowlist"])
    assert final["taskAdmission"]["status"] == "UNKNOWN"
    assert final["taskAdmission"]["applyAdmission"] == "CLOSED"
    assert final["resources"]["gpuIdleConclusion"] == "UNKNOWN"


def test_signed_release_selection_uses_official_attestation_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the canonical verifier's signed target tuple is accepted.

    中文：只接受正式 verifier 校验过签名的目标 tuple。
    """

    release_directory = tmp_path / "release"
    release_directory.mkdir()
    deb = release_directory / "cyrene_1.2.3_ubuntu-22.04_amd64.deb"
    deb.write_bytes(b"official immutable DEB bytes")
    deb_sha = native_acceptance.hashlib.sha256(deb.read_bytes()).hexdigest()
    target_id = "linux-ubuntu-22.04-x86_64-python-3.12"
    proof = {
        "targetId": target_id,
        "debSha256": deb_sha,
        "markerPath": "/usr/share/cyrene/native-install-contract-v1.json",
        "markerSha256": "b" * 64,
        "serviceArtifactsIndexPath": "/usr/share/cyrene/service-artifacts/index.json",
        "serviceArtifactsIndexSha256": "c" * 64,
        "maintainerScriptsSha256": {
            "postinst": "d" * 64,
            "prerm": "e" * 64,
            "postrm": "f" * 64,
        },
        "services": {
            component_id: {"componentId": component_id}
            for component_id in (
                "cyrene-catalyst",
                "cyrene-exchange",
                "cyrene-navigator",
                "cyrene-reactor",
                "cyrene-yield",
            )
        },
        "checks": {
            "serviceActivation": "deferred",
            "brokerAction": "preserve-existing",
            "oldRuntimeAction": "preserve",
            "maintainerScriptsStaticScan": "passed",
            "verifiedServiceBytesPreserved": "passed",
            "freshBrokerUnavailable": "fail-closed",
            "upgradeState": "preserve-existing",
            "activeRuntimePointers": "preserve-existing",
            "pinnedPrivateRuntime": "passed",
        },
    }
    manifest = {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "releaseId": "native-installer-preview-" + "2" * 40,
        "version": "1.2.3",
        "channel": "preview",
        "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/native-installer-release.yml",
        "run": {"id": 1234, "attempt": 1},
        "source": {
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "ref": "refs/heads/develop",
            "commit": "2" * 40,
        },
        "targets": [
            {
                "targetId": target_id,
                "assetName": deb.name,
                "sha256": deb_sha,
                "sizeBytes": deb.stat().st_size,
            }
        ],
    }
    receipt = {
        "schemaVersion": 1,
        "safeInitialization": {
            "verified": True,
            "mode": "stage-only-verified-published-bytes",
            "targets": [proof],
        },
    }
    receipt_path = release_directory / "native-installer-source-receipt-v1.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    manifest["sourceReceipt"] = {
        "assetName": receipt_path.name,
        "sha256": native_acceptance.hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "sizeBytes": receipt_path.stat().st_size,
    }
    (release_directory / "native-installer-release-v1.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    def verify_release_directory(directory: Path, **kwargs: object) -> dict[str, object]:
        assert directory == release_directory
        assert kwargs["expected_repository"] == "DoHorizon-AI/Cyrene-Workspace"
        assert kwargs["expected_source_ref"] == "refs/heads/develop"
        assert kwargs["expected_source_commit"] == "2" * 40
        assert kwargs["verify_attestations"] is True
        return manifest

    fake_helper = SimpleNamespace(
        PRODUCTS={component_id: ("repository", component_id) for component_id in proof["services"]},
        verify_release_directory=verify_release_directory,
        _read_json_object=lambda path, _label: json.loads(path.read_text(encoding="utf-8")),
    )
    loaded_roots: list[Path] = []

    def load_helper(workspace: Path) -> tuple[object, str]:
        loaded_roots.append(workspace)
        return fake_helper, "sha256:" + "9" * 64

    monkeypatch.setattr(native_acceptance, "_load_native_release_helper", load_helper)
    monkeypatch.setattr(
        native_acceptance,
        "_assert_release_helper_matches_source_commit",
        lambda _workspace, _commit: None,
    )

    result = native_acceptance.verify_native_release_for_host(
        release_directory,
        expected_source_ref="refs/heads/develop",
        expected_source_commit="2" * 40,
        ubuntu_version="22.04",
    )
    assert result["verifier"]["attestationsVerified"] is True
    assert result["target"]["debSha256"] == "sha256:" + deb_sha
    assert result["target"]["serviceArtifactsIndexSha256"] == "c" * 64
    assert loaded_roots == [WORKSPACE_ROOT]

    for check, bad_value in (
        ("freshBrokerUnavailable", None),
        ("upgradeState", "reset"),
        ("activeRuntimePointers", "replace"),
        ("pinnedPrivateRuntime", None),
    ):
        malformed_receipt = json.loads(json.dumps(receipt))
        if bad_value is None:
            malformed_receipt["safeInitialization"]["targets"][0]["checks"].pop(check)
        else:
            malformed_receipt["safeInitialization"]["targets"][0]["checks"][check] = bad_value
        receipt_path.write_text(json.dumps(malformed_receipt), encoding="utf-8")
        manifest["sourceReceipt"] = {
            "assetName": receipt_path.name,
            "sha256": native_acceptance.hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "sizeBytes": receipt_path.stat().st_size,
        }
        (release_directory / "native-installer-release-v1.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        with pytest.raises(native_acceptance.AcceptanceError, match="incomplete or non-canonical"):
            native_acceptance.verify_native_release_for_host(
                release_directory,
                expected_source_ref="refs/heads/develop",
                expected_source_commit="2" * 40,
                ubuntu_version="22.04",
            )


def test_release_verification_rejects_deb_changed_after_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selected DEB is rehashed after the official verifier returns.

    中文：正式 verifier 返回后再次校验所选 DEB 的字节摘要。
    """

    release_directory = tmp_path / "release"
    release_directory.mkdir()
    deb = release_directory / "cyrene_1.2.3_ubuntu-22.04_amd64.deb"
    deb.write_bytes(b"first version")
    profile = "linux-ubuntu-22.04-x86_64-python-3.12"
    proof = {
        "targetId": profile,
        "debSha256": native_acceptance.hashlib.sha256(deb.read_bytes()).hexdigest(),
        "markerPath": "/usr/share/cyrene/native-install-contract-v1.json",
        "markerSha256": "d" * 64,
        "serviceArtifactsIndexPath": "/usr/share/cyrene/service-artifacts/index.json",
        "serviceArtifactsIndexSha256": "c" * 64,
        "maintainerScriptsSha256": {
            "postinst": "e" * 64,
            "prerm": "f" * 64,
            "postrm": "a" * 64,
        },
        "services": {
            component_id: {"componentId": component_id}
            for component_id in (
                "cyrene-catalyst",
                "cyrene-exchange",
                "cyrene-navigator",
                "cyrene-reactor",
                "cyrene-yield",
            )
        },
        "checks": {
            "serviceActivation": "deferred",
            "brokerAction": "preserve-existing",
            "oldRuntimeAction": "preserve",
            "maintainerScriptsStaticScan": "passed",
            "verifiedServiceBytesPreserved": "passed",
            "freshBrokerUnavailable": "fail-closed",
            "upgradeState": "preserve-existing",
            "activeRuntimePointers": "preserve-existing",
            "pinnedPrivateRuntime": "passed",
        },
    }
    manifest = {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "releaseId": "release",
        "version": "1.2.3",
        "channel": "preview",
        "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/native-installer-release.yml",
        "run": {"id": 1, "attempt": 1},
        "source": {
            "ref": "refs/heads/develop",
            "commit": "2" * 40,
        },
        "targets": [
            {
                "targetId": profile,
                "assetName": deb.name,
                "sha256": native_acceptance.hashlib.sha256(deb.read_bytes()).hexdigest(),
                "sizeBytes": deb.stat().st_size,
            }
        ],
    }

    def verify_release_directory(_directory: Path, **_kwargs: object) -> dict[str, object]:
        deb.write_bytes(b"tampered after verifier")
        return manifest

    fake_helper = SimpleNamespace(
        PRODUCTS={component_id: ("repository", component_id) for component_id in proof["services"]},
        verify_release_directory=verify_release_directory,
        _read_json_object=lambda _path, _label: {
            "safeInitialization": {
                "verified": True,
                "mode": "stage-only-verified-published-bytes",
                "targets": [proof],
            }
        },
    )
    (release_directory / "native-installer-release-v1.json").write_text("{}", encoding="utf-8")
    (release_directory / "native-installer-source-receipt-v1.json").write_text(
        "{}", encoding="utf-8"
    )
    monkeypatch.setattr(
        native_acceptance,
        "_load_native_release_helper",
        lambda _workspace: (fake_helper, "sha256:" + "9" * 64),
    )
    monkeypatch.setattr(
        native_acceptance,
        "_assert_release_helper_matches_source_commit",
        lambda _workspace, _commit: None,
    )

    with pytest.raises(native_acceptance.AcceptanceError, match="changed after official"):
        native_acceptance.verify_native_release_for_host(
            release_directory,
            expected_source_ref="refs/heads/develop",
            expected_source_commit="2" * 40,
            ubuntu_version="22.04",
        )


def test_release_selection_requires_current_readonly_host_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator cannot choose another DEB target by supplying a profile.

    中文：operator 不得通过手填 profile 选择其他系统的 DEB。
    """

    run_path = _new_run(tmp_path)
    monkeypatch.setattr(
        native_acceptance,
        "verify_native_release_for_host",
        lambda *_args, **_kwargs: pytest.fail("release verifier must not run without host facts"),
    )
    with pytest.raises(native_acceptance.AcceptanceError, match="Collect an Ubuntu x86_64"):
        native_acceptance.record_verified_release(
            run_path,
            tmp_path / "release",
            expected_source_ref="refs/heads/develop",
            expected_source_commit="a" * 40,
        )


@pytest.mark.parametrize("alias", ["server;id", "host with spaces", "$(touch /tmp/x)"])
def test_ssh_alias_rejects_shell_syntax(alias: str) -> None:
    """SSH aliases must not be interpolated from shell syntax.

    中文：SSH alias 不得包含可注入的 shell 语法。
    """

    with pytest.raises(native_acceptance.AcceptanceError):
        native_acceptance.validate_ssh_alias(alias)
