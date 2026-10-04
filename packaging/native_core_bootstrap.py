"""First Core installation through the fixed native component updater.

The bootstrap keeps Kernel admission closed from the first Core pointer change
until the newly started Kernel reports known, empty runtime ownership counts.
中文：首次安装期间持久闭门；只有真实 Kernel 计数已知且为空时才解除闭门。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any

CORE_COMPONENT_IDS = (
    "cyrene-linux-sys-adapter",
    "cyrene-nvidia-adapter",
    "cyrene-sandboxd",
    "cyrene-kernel",
)
CORE_BOOTSTRAP_MODE = "first-core"
JOURNAL_NAME = "first-core-bootstrap.json"
CORE_EXECUTABLE_NAMES = frozenset(
    {"cyrene-linux-sys-adapter", "cyrene-nvidia-adapter", "cyrene-sandboxd", "cyrene-kernel"}
)
PROC_ROOT = Path("/proc")
CORE_RUNTIME_ROOT = Path("/var/lib/cyrene/runtime")
CORE_RUN_ROOT = Path("/run/cyrene")
_DELETED_EXE_SUFFIX = " (deleted)"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _journal_path(updater: Any) -> Path:
    return Path(updater.state_root) / "native-first-bootstrap" / JOURNAL_NAME


def _resume_first_products_post_end(
    updater: Any,
    plan: dict[str, Any],
    transaction: dict[str, Any],
    journal_path: Path,
    *,
    recovery: bool = True,
) -> dict[str, Any]:
    """Resume only Product post-End work; never roll back the released Core cohort."""

    if transaction.get("gateReleaseConfirmed") is not True:
        raise ValueError("Post-End Product phase has no durable Core gate-release proof")
    addon = transaction.get("firstProducts")
    identity = plan.get("firstProducts")
    if (
        not isinstance(addon, dict)
        or not isinstance(identity, dict)
        or addon.get("receiptDigest") != identity.get("receiptDigest")
        or addon.get("products") != identity.get("products")
    ):
        raise ValueError("Post-End Product journal no longer matches the immutable plan")
    try:
        module = _first_products_module()
        callback = module.recover_post_end if recovery else module.complete_post_end
        result = callback(updater, plan, transaction)
        if not isinstance(result, dict) or result.get("status") not in {"complete", "pending"}:
            raise TypeError("Product post-End recovery returned an invalid status")
    except Exception as error:
        transaction["phase"] = "post_end_pending"
        transaction["postEndError"] = str(error)[:500]
        _write_private_json(updater, journal_path, transaction)
        raise RuntimeError(
            "Core gate release is confirmed; Product post-End work remains pending and was not rolled back"
        ) from error
    transaction["postEndResult"] = result
    transaction["phase"] = "succeeded" if result["status"] == "complete" else "post_end_pending"
    transaction.pop("postEndError", None)
    _write_private_json(updater, journal_path, transaction)
    return {
        "status": "installed" if result["status"] == "complete" else "post-end-pending",
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "postEndReadiness": transaction.get("postEndReadiness", "UNKNOWN"),
        "firstProducts": result,
    }


def _rollback_pre_end(
    updater: Any,
    plan: dict[str, Any],
    transaction: dict[str, Any],
    components: list[dict[str, Any]],
) -> None:
    """Remove Product-owned candidates first, then this transaction's Core cohort."""

    if plan.get("includeProducts") is True:
        record = transaction.get("firstProducts")
        if not isinstance(record, dict):
            raise ValueError("First-Product ownership journal is missing before cleanup")
        has_owned_products = (
            any(record.get(key) for key in ("ownedPointers", "ownedUnits", "ownedPids"))
            or record.get("pendingPointer") is not None
            or record.get("pendingUnit") is not None
        )
        if has_owned_products or record.get("phase") not in {"planned", "staged"}:
            _first_products_module().rollback_pre_end(updater, plan, transaction)
    _stop_candidate_services(updater, components)
    _remove_candidate_pointers_and_units(updater, components)


def _read_private_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("First-Core journal or plan path is unsafe")
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("First-Core journal or plan is not private to the updater owner")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("First-Core journal or plan is malformed")
    return value


def _write_private_json(updater: Any, path: Path, value: dict[str, Any]) -> None:
    updater._ensure_state_root()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.parent.is_symlink()
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("First-Core private state directory is unsafe")
    updater._atomic_json_file(path, value, mode=0o600)


def _read_process_executable(exe_link: Path) -> tuple[str, str]:
    """Read a procfs executable link and validate its live inode.

    `/proc/PID/exe` remains stat-able after unlink, unlike the displayed host
    pathname. Keep the suffix in the returned path so deleted Core binaries
    can never match a planned candidate executable.
    中文：校验 procfs 中仍存活的 inode，并让已删除 Core 可执行文件继续触发旧进程门禁。
    """

    target = os.readlink(exe_link)
    path_for_name = target.removesuffix(_DELETED_EXE_SUFFIX)
    if not Path(path_for_name).is_absolute():
        raise OSError(errno.EINVAL, "process executable path is not absolute", str(exe_link))
    metadata = os.stat(exe_link)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(errno.EINVAL, "process executable is not a regular file", str(exe_link))
    name = Path(path_for_name).name
    if not name:
        raise OSError(errno.EINVAL, "process executable name is empty", str(exe_link))
    return target, name


def _core_process_snapshot(proc_root: Path = PROC_ROOT) -> list[tuple[str, str]]:
    """Inspect every process and retain exact executable paths for recovery checks."""

    if not proc_root.is_dir() or proc_root.is_symlink():
        raise RuntimeError("Process inventory is unavailable; first-Core eligibility is UNKNOWN")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as error:
        raise RuntimeError(
            "Process inventory is unreadable; first-Core eligibility is UNKNOWN"
        ) from error
    found: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            state = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[0]
            if state in {"Z", "X"}:
                continue
            command = (entry / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            if not entry.exists():
                continue
            raise RuntimeError(
                "Process inventory changed while checking first-Core eligibility"
            ) from None
        except (OSError, IndexError) as error:
            if isinstance(error, OSError) and error.errno == 2 and not entry.exists():
                continue
            raise RuntimeError(
                "Process inventory is incomplete; first-Core eligibility is UNKNOWN"
            ) from error
        names: set[str] = set()
        try:
            executable_path, executable_name = _read_process_executable(entry / "exe")
            names.add(executable_name)
        except FileNotFoundError:
            if not entry.exists():
                continue
            if any(command):
                raise RuntimeError(
                    "Process executable is unavailable; first-Core eligibility is UNKNOWN"
                ) from None
            continue  # Linux kernel threads have no executable or command line.
        except OSError as error:
            raise RuntimeError(
                "Process executable is unreadable; first-Core eligibility is UNKNOWN"
            ) from error
        for argument in command:
            if argument:
                names.add(Path(os.fsdecode(argument)).name)
        found.extend((name, executable_path) for name in sorted(names & CORE_EXECUTABLE_NAMES))
    return found


def _core_processes(proc_root: Path = PROC_ROOT) -> list[str]:
    """Return legacy Core process names after validating the whole process table."""

    return sorted({name for name, _ in _core_process_snapshot(proc_root)})


def _assert_fresh(
    updater: Any,
    *,
    proc_root: Path = PROC_ROOT,
    planned_components: list[dict[str, Any]] | None = None,
    require_empty_kernel_state: bool = True,
) -> None:
    """Require a genuine empty first install; never take over old Core state."""

    planned = {item["componentId"]: item for item in planned_components or []}
    expected_executables: set[str] = set()
    for component_id in CORE_COMPONENT_IDS:
        component = updater.components.get(component_id)
        if not isinstance(component, dict):
            raise TypeError(f"Trusted catalog omits Core component {component_id}")
        item = planned.get(component_id)
        expected_pointer = None
        if item is not None:
            expected_pointer = (
                f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
            )
            entrypoint = item.get("manifest", {}).get("artifact", {}).get("entrypoint")
            if not isinstance(entrypoint, str):
                raise ValueError("Staged Core entrypoint identity is incomplete")
            expected_executables.add(
                str(
                    (
                        updater.install_root
                        / "components"
                        / component_id
                        / "releases"
                        / expected_pointer
                        / entrypoint
                    ).resolve()
                )
            )
        current = updater._active_native_pointer_identity(component_id)
        if current is not None and current != expected_pointer:
            raise ValueError(
                f"Core component {component_id} already has an unrelated active pointer"
            )
        unit = component.get("systemdUnit")
        if not isinstance(unit, str):
            raise TypeError(f"Trusted catalog has no fixed unit for {component_id}")
        for directory in updater.systemd_unit_dirs:
            path = Path(directory) / unit
            if path.exists() or path.is_symlink():
                if item is None:
                    raise ValueError(f"Existing Core unit blocks first install: {unit}")
                release = (
                    updater.install_root
                    / "components"
                    / component_id
                    / "releases"
                    / expected_pointer
                )
                candidate_unit = release / "systemd" / unit
                if (
                    path.is_symlink()
                    or not candidate_unit.is_file()
                    or path.read_bytes() != candidate_unit.read_bytes()
                ):
                    raise ValueError(f"Existing Core unit differs from this bootstrap plan: {unit}")
    if require_empty_kernel_state:
        runtime_root = Path(getattr(updater, "core_runtime_root", CORE_RUNTIME_ROOT))
        run_root = Path(getattr(updater, "core_run_root", CORE_RUN_ROOT))
        for root in (runtime_root, run_root):
            try:
                info = root.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise RuntimeError(
                    "Kernel runtime ownership inventory is unavailable; first-Core eligibility is UNKNOWN"
                ) from error
            if root.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(
                    "Kernel runtime ownership root is unsafe; first-Core eligibility is UNKNOWN"
                )
        kernel_journal = runtime_root / "journal.jsonl"
        if kernel_journal.exists() or kernel_journal.is_symlink():
            raise ValueError("Existing Kernel runtime ownership journal blocks first install")
        for relative in (
            Path("workers"),
            Path("kernel.sock"),
            Path("worker.sock"),
            Path("provider.sock"),
            Path("linux-sys-adapter.sock"),
            Path("nvidia-adapter.sock"),
            Path("sandboxd.sock"),
        ):
            path = run_root / relative
            if path.exists() or path.is_symlink():
                raise ValueError(
                    f"Existing Core runtime ownership path blocks first install: {relative}"
                )
    processes = _core_process_snapshot(proc_root)
    if any(executable not in expected_executables for _, executable in processes):
        raise ValueError("Legacy or manually started Core process blocks first install")
    try:
        gpu = updater.runner(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, TimeoutError) as error:
        raise RuntimeError(
            "GPU resource inventory is unavailable; first-Core eligibility is UNKNOWN"
        ) from error
    if gpu.returncode != 0:
        raise RuntimeError("GPU resource inventory failed; first-Core eligibility is UNKNOWN")
    if gpu.stdout.strip():
        raise ValueError("Existing GPU compute resources block first-Core installation")


def _health_snapshot(updater: Any, *, require_eligible: bool = True) -> dict[str, Any]:
    result = updater._broker_request("Health", {})
    if (
        result.get("status") != "SERVING"
        or not isinstance(result.get("gate_generation"), int)
        or isinstance(result.get("gate_generation"), bool)
        or not isinstance(result.get("catalog_generation"), int)
        or isinstance(result.get("catalog_generation"), bool)
    ):
        raise RuntimeError("Runtime maintenance broker Health identity is unknown")
    eligible = result.get("core_bootstrap_eligible")
    if not isinstance(eligible, bool):
        raise TypeError("Platform did not expose a known first-Core eligibility state")
    if require_eligible and not eligible:
        raise RuntimeError("Platform does not confirm a fresh first-Core bootstrap state")
    activity_catalog, sources = updater._activity_catalog()
    if activity_catalog["generation"] != result["catalog_generation"]:
        raise RuntimeError("Broker and installed activity catalog generations differ")
    return {
        "gateGeneration": result["gate_generation"],
        "catalogGeneration": result["catalog_generation"],
        "activitySources": sources,
        "coreBootstrapEligible": eligible,
    }


def _plan_path(updater: Any, plan_id: str) -> Path:
    return Path(updater.state_root) / "plans" / (plan_id + ".first-core.json")


def _base_plan(updater: Any, plan_id: str, plan_digest: str) -> dict[str, Any]:
    """Load the ordinary updater plan bound as the Core portion of this plan."""

    path = Path(updater.state_root) / "plans" / (plan_id + ".json")
    value = _read_private_json(path)
    if value is None or value.get("planId") != plan_id or value.get("planDigest") != plan_digest:
        raise ValueError("The fixed Core update plan changed; check again")
    return value


def _first_products_module() -> Any:
    """Load the fixed Product first-start helper shipped beside this module."""

    name = "_cyrene_native_first_products"
    module = sys.modules.get(name)
    if module is not None:
        return module
    import importlib.util

    helper_path = Path(__file__).with_name("native_first_products.py")
    if not helper_path.is_file() or helper_path.is_symlink():
        raise RuntimeError("The fixed first-Product helper is unavailable")
    spec = importlib.util.spec_from_file_location(name, helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The fixed first-Product helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_plan(updater: Any, plan_id: Any, plan_digest: Any) -> dict[str, Any]:
    updater._validate_plan_identity(plan_id, plan_digest)
    value = _read_private_json(_plan_path(updater, plan_id))
    if value is None or value.get("planId") != plan_id or value.get("planDigest") != plan_digest:
        raise ValueError("First-Core plan identity changed; check again")
    if value.get("mode") != CORE_BOOTSTRAP_MODE:
        raise ValueError("Plan is not a first-Core bootstrap plan")
    updater_module = sys.modules.get(updater.__class__.__module__)
    canonical = getattr(updater_module, "canonical_jcs", None)
    if not callable(canonical):
        canonical = next(
            (
                module.canonical_jcs
                for module in tuple(sys.modules.values())
                if callable(getattr(module, "canonical_jcs", None))
            ),
            None,
        )
    if not callable(canonical):
        raise TypeError("Trusted plan canonicalization is unavailable")
    include_products = value.get("includeProducts", False)
    if not isinstance(include_products, bool):
        raise TypeError("First-Core includeProducts plan field is not a boolean")
    material = {
        key: value[key]
        for key in (
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
        )
    }
    # Accept Core-only plans written before the explicit Product opt-in field.
    if "includeProducts" in value:
        material["includeProducts"] = include_products
    if include_products:
        first_products = value.get("firstProducts")
        if not isinstance(first_products, dict) or first_products.get("schemaVersion") != 1:
            raise ValueError("First-Product identity block is missing from the immutable plan")
        material["firstProducts"] = first_products
    elif "firstProducts" in value:
        raise ValueError("Core-only plan unexpectedly contains Product cohort data")
    expected_digest = _digest(canonical(material))
    if expected_digest != plan_digest or plan_id != "plan-" + expected_digest.split(":", 1)[1][:32]:
        raise ValueError("First-Core plan digest does not match its immutable cohort data")
    expected_artifacts = {
        item["componentId"]: item["artifactDigest"] for item in value.get("components", [])
    }
    if value.get("componentArtifactDigests") != expected_artifacts:
        raise ValueError("First-Core artifact digest map differs from its immutable cohort")
    return value


def check(
    updater: Any,
    *,
    channel: Any = None,
    include_products: bool = False,
    proc_root: Path = PROC_ROOT,
) -> dict[str, Any]:
    """Create a digest-bound four-component bootstrap plan from trusted candidates."""

    updater._reload_catalog_for_operation()
    updater._ensure_state_root()
    if not isinstance(include_products, bool):
        raise TypeError("includeProducts must be a boolean")
    _assert_fresh(updater, proc_root=proc_root)
    snapshot = _health_snapshot(updater)
    checked = updater.check(list(CORE_COMPONENT_IDS), channel=channel, include_readiness=False)
    normal = checked.get("plan")
    if not isinstance(normal, dict):
        raise TypeError("Trusted release index did not produce a complete Core cohort")
    items = normal.get("components")
    if not isinstance(items, list) or {item.get("componentId") for item in items} != set(
        CORE_COMPONENT_IDS
    ):
        raise ValueError("Trusted release index did not produce exactly the four Core components")
    targets = {
        updater._target_for(updater.components[component_id]).get("id")
        for component_id in CORE_COMPONENT_IDS
    }
    if len(targets) != 1 or None in targets:
        raise ValueError("Core artifacts do not share one supported native target")
    first_products = _first_products_module().check(updater, normal) if include_products else None
    material = {
        "schemaVersion": 1,
        "mode": CORE_BOOTSTRAP_MODE,
        "basePlanId": normal["planId"],
        "basePlanDigest": normal["planDigest"],
        "channel": normal["channel"],
        "catalogGeneration": normal["catalogGeneration"],
        "catalogDigest": normal["catalogDigest"],
        **snapshot,
        "targetId": next(iter(targets)),
        "components": sorted(items, key=lambda item: item["componentId"]),
        "includeProducts": include_products,
    }
    if include_products:
        if not isinstance(first_products, dict) or first_products.get("schemaVersion") != 1:
            raise TypeError("First-Product helper returned an invalid immutable identity block")
        material["firstProducts"] = first_products
    updater_module = sys.modules.get(updater.__class__.__module__)
    canonical = getattr(updater_module, "canonical_jcs", None)
    if not callable(canonical):
        canonical = next(
            (
                module.canonical_jcs
                for module in tuple(sys.modules.values())
                if callable(getattr(module, "canonical_jcs", None))
            ),
            None,
        )
    if not callable(canonical):
        raise TypeError("Trusted updater canonical JSON implementation is unavailable")
    plan_digest = _digest(canonical(material))
    plan_id = "plan-" + plan_digest.split(":", 1)[1][:32]
    request_id = "bootstrap-" + plan_digest.split(":", 1)[1][:32]
    plan = {
        **material,
        "planId": plan_id,
        "planDigest": plan_digest,
        "requestId": request_id,
        "phase": "checked",
        "requiresRestart": True,
        "userConfirmedRestart": True,
        "componentArtifactDigests": {
            item["componentId"]: item["artifactDigest"] for item in material["components"]
        },
    }
    _write_private_json(updater, _plan_path(updater, plan_id), plan)
    return {"status": "checked", "mode": CORE_BOOTSTRAP_MODE, "plan": plan, "plans": [plan]}


def stage(updater: Any, plan_id: Any, plan_digest: Any, *, channel: Any = None) -> dict[str, Any]:
    """Stage every exact signed candidate while allowing runtime readiness UNKNOWN."""

    plan = _load_plan(updater, plan_id, plan_digest)
    snapshot = _health_snapshot(updater, require_eligible=False)
    if any(
        plan.get(key) != value
        for key, value in snapshot.items()
        if key in {"catalogGeneration", "activitySources"}
    ):
        raise ValueError("Core bootstrap gate or activity catalog changed; check again")
    staged = updater.stage(
        plan["basePlanId"], plan["basePlanDigest"], channel=channel or plan["channel"]
    )
    base_stage = Path(updater.state_root) / "staged" / plan["basePlanId"] / "stage.json"
    record = _read_private_json(base_stage)
    if record is None or record.get("phase") != "staged":
        raise ValueError("Verified Core cohort was not completely staged")
    digests = {
        item.get("componentId"): item.get("artifactDigest")
        for item in record.get("components", [])
        if isinstance(item, dict)
    }
    if digests != plan["componentArtifactDigests"]:
        raise ValueError("Staged Core artifact digests differ from the confirmed plan")
    first_products_stage = None
    if plan.get("includeProducts") is True:
        base_plan = _base_plan(updater, plan["basePlanId"], plan["basePlanDigest"])
        first_products_stage = _first_products_module().stage(
            updater, base_plan, plan["firstProducts"]
        )
        if (
            not isinstance(first_products_stage, dict)
            or first_products_stage.get("receiptDigest") != plan["firstProducts"]["receiptDigest"]
            or first_products_stage.get("products") != plan["firstProducts"]["products"]
        ):
            raise ValueError("Staged Product cohort differs from the immutable check plan")
    staged_plan = {**plan, "phase": "staged"}
    staged_record = {
        "schemaVersion": 1,
        "mode": CORE_BOOTSTRAP_MODE,
        "plan": staged_plan,
        "basePlanId": plan["basePlanId"],
        "basePlanDigest": plan["basePlanDigest"],
        "phase": "staged",
    }
    if first_products_stage is not None:
        staged_record["firstProductsStage"] = first_products_stage
    _write_private_json(
        updater, Path(updater.state_root) / "staged" / plan_id / "stage.json", staged_record
    )
    return {
        "status": "staged",
        "mode": CORE_BOOTSTRAP_MODE,
        "plan": staged_plan,
        "plans": [staged_plan],
        "components": staged.get("components", []),
        **({"firstProducts": first_products_stage} if first_products_stage is not None else {}),
    }


def _write_unit(updater: Any, component: dict[str, Any], release: Path) -> Path:
    unit = component["systemdUnit"]
    source = release / "systemd" / unit
    if (
        source.is_symlink()
        or not source.is_file()
        or not updater._unit_uses_component_runner(source, component["componentId"])
    ):
        raise ValueError(f"Verified Core release has no catalog-matched unit: {unit}")
    if not updater.systemd_unit_dirs:
        raise ValueError("First-Core bootstrap has no fixed systemd unit directory")
    # The normal catalog resolver searches /etc before vendor directories. Put
    # managed overrides in that first, effective location while preserving the
    # updater's full search path for conflict checks.
    destination = Path(updater.systemd_unit_dirs[0]) / unit
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if not destination.is_symlink() and destination.read_bytes() == source.read_bytes():
            return destination
        raise ValueError(f"Core unit path became occupied before installation: {unit}")
    temporary = destination.with_name("." + destination.name + ".first-core.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("Interrupted Core unit temporary path requires operator review")
    shutil.copyfile(source, temporary)
    temporary.chmod(0o644)
    os.replace(temporary, destination)
    updater._fsync_directory(destination.parent)
    return destination


def _require_core_ready(updater: Any, plan: dict[str, Any]) -> dict[str, Any]:
    activity_catalog, sources = updater._activity_catalog()
    if (
        activity_catalog.get("generation") != plan["catalogGeneration"]
        or sources != plan["activitySources"]
    ):
        raise RuntimeError("Installed activity catalog changed while the first-Core hold is closed")
    result = updater._readiness_for("CORE_RUNTIME", requires_restart=True, force=True)
    blockers = result.get("blocker_codes")
    zero_counts = (
        "active_task_count",
        "inflight_runtime_admission_count",
        "active_worker_count",
        "active_allocation_count",
    )
    if (
        not isinstance(blockers, list)
        or "RUNTIME_ACTIVITY_UNKNOWN" in blockers
        or any(
            not isinstance(result.get(field), int)
            or isinstance(result.get(field), bool)
            or result.get(field) != 0
            for field in zero_counts
        )
        or not isinstance(result.get("active_tasks"), list)
        or result.get("active_tasks")
    ):
        raise RuntimeError(
            "Kernel runtime ownership counts are unknown or non-empty; the first-Core hold remains closed"
        )
    return result


def _verify_live_core_cohort(
    updater: Any, transaction: dict[str, Any], plan: dict[str, Any]
) -> None:
    """Prove all four exact staged releases are active and their units are healthy."""

    if {item.get("componentId") for item in transaction.get("components", [])} != set(
        CORE_COMPONENT_IDS
    ):
        raise RuntimeError("Bootstrap journal no longer contains the exact four-component cohort")
    for item in transaction["components"]:
        component_id = item["componentId"]
        pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
        if updater._active_native_pointer_identity(component_id) != pointer:
            raise RuntimeError(
                f"Active pointer no longer matches the confirmed Core plan: {component_id}"
            )
        component = updater.components[component_id]
        units = [
            Path(directory) / component["systemdUnit"] for directory in updater.systemd_unit_dirs
        ]
        if not units or units[0].is_symlink() or not units[0].is_file():
            raise RuntimeError(
                f"Catalog-owned systemd unit is missing or ambiguous: {component_id}"
            )
        release = updater.install_root / "components" / component_id / "releases" / pointer
        source = release / "systemd" / component["systemdUnit"]
        if not source.is_file() or units[0].read_bytes() != source.read_bytes():
            raise RuntimeError(
                f"Installed systemd unit differs from its staged release: {component_id}"
            )
        for shadow in units[1:]:
            if (shadow.exists() or shadow.is_symlink()) and (
                shadow.is_symlink()
                or not shadow.is_file()
                or shadow.read_bytes() != source.read_bytes()
            ):
                raise RuntimeError(
                    f"A conflicting shadow unit exists in another systemd search path: {component_id}"
                )
    _verify_started_processes(updater, transaction["components"])
    updater._health_transaction(transaction)
    snapshot, sources = _health_snapshot(updater, require_eligible=False), plan["activitySources"]
    if (
        snapshot["catalogGeneration"] != plan["catalogGeneration"]
        or snapshot["activitySources"] != sources
    ):
        raise RuntimeError("Platform activity catalog changed while the Core hold is active")


def _verify_started_processes(updater: Any, components: list[dict[str, Any]]) -> None:
    """Require each managed unit to own a live PID running its catalog binary."""

    for item in components:
        component = updater.components[item["componentId"]]
        unit = component["systemdUnit"]
        completed = updater.runner(
            ["systemctl", "show", "--property=MainPID", "--value", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip().isdecimal():
            raise RuntimeError(f"{unit} has no confirmed systemd MainPID")
        pid = completed.stdout.strip()
        _verify_candidate_pid(updater, item, pid)


def _candidate_executable(updater: Any, item: dict[str, Any]) -> Path:
    """Validate root-owned bytes at the exact attested manifest entrypoint."""

    manifest = item.get("manifest")
    artifact = manifest.get("artifact") if isinstance(manifest, dict) else None
    entrypoint = artifact.get("entrypoint") if isinstance(artifact, dict) else None
    files = artifact.get("files") if isinstance(artifact, dict) else None
    if not isinstance(entrypoint, str) or not isinstance(files, dict):
        raise TypeError("Staged native artifact has no exact entrypoint ownership proof")
    component_id = item["componentId"]
    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
    executable = (
        updater.install_root / "components" / component_id / "releases" / pointer / entrypoint
    )
    info = executable.lstat()
    if (
        executable.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
        or _digest(executable.read_bytes()) != files.get(entrypoint)
    ):
        raise RuntimeError("Core entrypoint ownership or digest differs from the staged artifact")
    return executable.resolve(strict=True)


def _verify_candidate_pid(updater: Any, item: dict[str, Any], pid: str) -> None:
    expected = _candidate_executable(updater, item)
    executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
    if executable != expected:
        raise RuntimeError(
            f"{updater.components[item['componentId']]['systemdUnit']} MainPID is not the exact staged release entrypoint"
        )


def _stop_candidate_services(updater: Any, components: list[dict[str, Any]]) -> None:
    """Stop only exact plan-owned unit PIDs, in reverse dependency order."""

    ordered = sorted(
        components,
        key=lambda item: updater.components[item["componentId"]]["restart"]["order"],
        reverse=True,
    )
    for item in ordered:
        unit = updater.components[item["componentId"]]["systemdUnit"]
        completed = updater.runner(
            ["systemctl", "show", "--property=MainPID", "--value", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip().isdecimal():
            raise RuntimeError(
                f"Cannot prove the owned PID for {unit}; leave candidate files and hold intact"
            )
        pid = completed.stdout.strip()
        if pid == "0":
            continue
        _verify_candidate_pid(updater, item, pid)
        updater._run_systemctl("stop", unit)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = updater.runner(
                ["systemctl", "show", "--property=MainPID", "--value", unit],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if state.returncode == 0 and state.stdout.strip() == "0":
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Owned candidate unit {unit} did not stop; keep the hold and files")


def _activate_core_cohort(updater: Any, transaction: dict[str, Any]) -> None:
    """Activate only the four plan-bound pointers, resuming exact partial work."""

    for item in transaction["components"]:
        component_id = item["componentId"]
        pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
        current = updater._active_native_pointer_identity(component_id)
        if current not in {None, pointer}:
            raise ValueError(
                f"Core pointer changed outside this bootstrap transaction: {component_id}"
            )
        if current is None:
            previous = next(
                entry for entry in transaction["previous"] if entry["componentId"] == component_id
            )
            updater._activate_transaction(
                {**transaction, "components": [item], "previous": [previous]}
            )
        else:
            updater._write_active_receipt(item)


def _start_core_components(updater: Any, components: list[dict[str, Any]]) -> None:
    """Start only the exact four new Core units in catalog order."""

    ordered = sorted(
        components, key=lambda item: updater.components[item["componentId"]]["restart"]["order"]
    )
    if [item["componentId"] for item in ordered] != list(CORE_COMPONENT_IDS):
        raise ValueError("Core catalog start order differs from the fixed first-Core cohort")
    for item in ordered:
        unit = updater.components[item["componentId"]]["systemdUnit"]
        updater._run_systemctl("start", unit)
        updater._wait_unit_active(unit)
        _verify_started_processes(updater, [item])


def _remove_candidate_pointers_and_units(updater: Any, components: list[dict[str, Any]]) -> None:
    """Remove only pointers and units proven to belong to this staged cohort."""

    for item in reversed(components):
        component_id = item["componentId"]
        component = updater.components[component_id]
        pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
        current = updater._active_native_pointer_identity(component_id)
        if current == pointer:
            updater._activate_native(component_id, None, expected_current=pointer)
            updater._clear_active_receipt(component_id)
        unit_path = next(
            (
                Path(directory) / component["systemdUnit"]
                for directory in updater.systemd_unit_dirs
                if (Path(directory) / component["systemdUnit"]).exists()
            ),
            None,
        )
        if unit_path is not None and not unit_path.is_symlink():
            release = updater.install_root / "components" / component_id / "releases" / pointer
            source = release / "systemd" / component["systemdUnit"]
            if source.is_file() and unit_path.read_bytes() == source.read_bytes():
                unit_path.unlink()
                updater._fsync_directory(unit_path.parent)


def apply(
    updater: Any,
    plan_id: Any,
    plan_digest: Any,
    confirmation: Any,
    *,
    channel: Any = None,
    proc_root: Path = PROC_ROOT,
) -> dict[str, Any]:
    """Install the confirmed Core cohort under a durable hold, preserving it on failure."""

    plan = _load_plan(updater, plan_id, plan_digest)
    expected_confirmation = {
        "mode": CORE_BOOTSTRAP_MODE,
        "planId": plan_id,
        "planDigest": plan_digest,
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    if plan.get("includeProducts") is True:
        expected_confirmation["includeProducts"] = True
        expected_confirmation["firstProducts"] = plan["firstProducts"]
    if confirmation != expected_confirmation:
        raise ValueError(
            "Apply confirmation must bind the exact first-Core plan and full artifact cohort"
        )
    if channel is not None and updater._resolve_channel(channel) != plan["channel"]:
        raise ValueError("Apply channel differs from the staged first-Core plan")
    if not _is_root():
        raise PermissionError("First-Core activation requires the root-authorized updater")
    journal_path = _journal_path(updater)
    with updater._exclusive_update_lock():
        staged_path = Path(updater.state_root) / "staged" / plan_id / "stage.json"
        staged = _read_private_json(staged_path)
        if (
            staged is None
            or staged.get("phase") != "staged"
            or staged.get("plan") != {**plan, "phase": "staged"}
        ):
            raise ValueError("The exact confirmed Core bootstrap plan is not staged")
        if plan.get("includeProducts") is True:
            product_stage = staged.get("firstProductsStage")
            if (
                not isinstance(product_stage, dict)
                or product_stage.get("receiptDigest") != plan["firstProducts"]["receiptDigest"]
                or product_stage.get("products") != plan["firstProducts"]["products"]
            ):
                raise ValueError("The exact confirmed Product cohort is not staged")
        base_path = Path(updater.state_root) / "staged" / plan["basePlanId"] / "stage.json"
        base_record = _read_private_json(base_path)
        if base_record is None:
            raise ValueError("Verified Core artifact stage is missing")
        updater._validate_staged_record(
            base_record,
            expected_plan_id=plan["basePlanId"],
            expected_plan_digest=plan["basePlanDigest"],
        )
        components = base_record["components"]
        transaction = {
            "schemaVersion": 1,
            "requestId": plan["requestId"],
            "planId": plan_id,
            "planDigest": plan_digest,
            "targetKind": "CORE_RUNTIME",
            "expectedGateGeneration": plan["gateGeneration"],
            "expectedCatalogGeneration": plan["catalogGeneration"],
            "expectedActivitySources": plan["activitySources"],
            "componentArtifactDigests": plan["componentArtifactDigests"],
            "includeProducts": plan.get("includeProducts", False),
            **(
                {
                    "firstProducts": {
                        "schemaVersion": 1,
                        "receiptDigest": plan["firstProducts"]["receiptDigest"],
                        "products": plan["firstProducts"]["products"],
                        "phase": "staged",
                    }
                }
                if plan.get("includeProducts") is True
                else {}
            ),
            "components": components,
            "previous": updater._capture_active_versions(components),
            "beginRequest": {
                "request_id": plan["requestId"],
                "target_kind": "CORE_RUNTIME",
                "requires_restart": True,
                "user_confirmed_restart": True,
                "expected_gate_generation": plan["gateGeneration"],
                "expected_catalog_generation": plan["catalogGeneration"],
                "expected_activity_sources": plan["activitySources"],
                "plan_id": plan_id,
                "plan_digest": plan_digest,
                "component_artifact_digests": plan["componentArtifactDigests"],
            },
            "phase": "begin_pending",
            "createdAt": int(time.time()),
        }
        existing = _read_private_json(journal_path)
        if existing is not None:
            if (
                existing.get("planDigest") != plan_digest
                or existing.get("requestId") != plan["requestId"]
            ):
                raise ValueError(
                    "A different interrupted first-Core bootstrap keeps the gate closed"
                )
            for key in (
                "requestId",
                "planId",
                "planDigest",
                "targetKind",
                "expectedGateGeneration",
                "expectedCatalogGeneration",
                "expectedActivitySources",
                "componentArtifactDigests",
                "beginRequest",
                "components",
            ):
                if existing.get(key) != transaction.get(key):
                    raise ValueError(
                        f"Interrupted first-Core journal changed immutable field {key}"
                    )
            if (
                "includeProducts" in plan
                and existing.get("includeProducts", False) != plan["includeProducts"]
            ):
                raise ValueError("Interrupted first-Core journal changed Product selection")
            if plan.get("includeProducts") is True:
                previous_products = existing.get("firstProducts")
                expected_products = transaction["firstProducts"]
                if (
                    not isinstance(previous_products, dict)
                    or previous_products.get("receiptDigest") != expected_products["receiptDigest"]
                    or previous_products.get("products") != expected_products["products"]
                ):
                    raise ValueError("Interrupted Product journal changed its immutable identity")
            elif "firstProducts" in existing:
                raise ValueError("Core-only journal unexpectedly contains Product ownership")
            if existing.get("phase") == "succeeded":
                if plan.get("includeProducts") is True and (
                    not isinstance(existing.get("postEndResult"), dict)
                    or existing["postEndResult"].get("status") != "complete"
                ):
                    raise ValueError("Completed Product bootstrap journal lacks completion proof")
                return {
                    "status": "installed",
                    "planId": plan_id,
                    "planDigest": plan_digest,
                    "postEndReadiness": existing.get("postEndReadiness", "UNKNOWN"),
                    **(
                        {"firstProducts": existing["postEndResult"]}
                        if plan.get("includeProducts") is True
                        else {}
                    ),
                }
            if existing.get("phase") == "post_end_pending" and plan.get("includeProducts") is True:
                return _resume_first_products_post_end(updater, plan, existing, journal_path)
            if existing.get("phase") == "end_confirmed":
                if existing.get("gateReleaseConfirmed") is not True:
                    raise ValueError("End-confirmed journal has no durable unlock proof")
                if plan.get("includeProducts") is True:
                    existing["phase"] = "post_end_pending"
                    _write_private_json(updater, journal_path, existing)
                    return _resume_first_products_post_end(updater, plan, existing, journal_path)
                try:
                    post_end = updater._readiness_for(
                        "CORE_RUNTIME", requires_restart=True, force=True
                    )
                    existing["postEndReadiness"] = post_end.get("status", "UNKNOWN")
                except Exception as error:  # noqa: BLE001 - gate release is already confirmed.
                    existing["postEndReadiness"] = "UNKNOWN"
                    existing["postEndReadinessError"] = str(error)[:300]
                existing["phase"] = "succeeded"
                _write_private_json(updater, journal_path, existing)
                return {
                    "status": "installed",
                    "planId": plan_id,
                    "planDigest": plan_digest,
                    "postEndReadiness": existing["postEndReadiness"],
                }
            if any(
                item.get("pointerIdentity") is not None for item in existing.get("previous", [])
            ):
                raise ValueError("First-Core journal records an old Core pointer")
            if not isinstance(existing.get("maintenanceToken"), str) and existing.get(
                "phase"
            ) not in {"begin_pending", "end_confirmed"}:
                raise ValueError("Interrupted first-Core journal has no durable hold token")
            transaction = existing
        if existing is None or existing.get("phase") not in {
            "end_call_pending",
            "end_confirmed",
            "succeeded",
        }:
            _assert_fresh(
                updater,
                proc_root=proc_root,
                planned_components=components
                if existing is not None and existing.get("phase") != "begin_pending"
                else None,
                require_empty_kernel_state=existing is None
                or existing.get("phase") == "begin_pending",
            )
        if existing is None:
            _write_private_json(updater, journal_path, transaction)
        if transaction.get("phase") == "succeeded":
            return {"status": "installed", "planId": plan_id, "planDigest": plan_digest}
        if transaction.get("phase") == "begin_pending":
            request = transaction["beginRequest"]
            response = updater._broker_request(
                "BeginCoreBootstrap", request, request_id=plan["requestId"]
            )
            if (
                response.get("status") != "MAINTENANCE_ACTIVE"
                or response.get("maintenance_origin") != "CORE_BOOTSTRAP"
                or response.get("readiness_claimed") is not False
                or response.get("held") is not True
                or not isinstance(response.get("maintenance_token"), str)
                or not response.get("maintenance_token", "").strip()
                or not isinstance(response.get("gate_generation"), int)
                or isinstance(response.get("gate_generation"), bool)
            ):
                raise RuntimeError("Platform did not confirm a durable closed first-Core hold")
            transaction["maintenanceToken"] = response["maintenance_token"]
            transaction["phase"] = "held"
            _write_private_json(updater, journal_path, transaction)
        if transaction.get("phase") not in {
            "held",
            "installing",
            "starting",
            "started",
            "end_pending",
            "end_call_pending",
            "hold_required",
        }:
            raise ValueError(
                "Interrupted first-Core journal has an unknown phase; hold remains closed"
            )

        # ── Phase 1: Install the exact staged cohort while admission is closed.
        # 第一阶段：先持久闭门，再安装并启动本次签名Core组。
        try:
            if transaction["phase"] in {"held", "hold_required"}:
                transaction["phase"] = "installing"
                _write_private_json(updater, journal_path, transaction)
            if transaction["phase"] == "installing":
                for item in components:
                    component = updater.components[item["componentId"]]
                    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
                    release = (
                        updater.install_root
                        / "components"
                        / item["componentId"]
                        / "releases"
                        / pointer
                    )
                    _write_unit(updater, component, release)
                _activate_core_cohort(updater, transaction)
                completed = updater.runner(
                    ["systemctl", "daemon-reload"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError("systemd daemon-reload failed")
                transaction["phase"] = "starting"
                _write_private_json(updater, journal_path, transaction)
            if transaction["phase"] in {"starting", "started"}:
                _start_core_components(updater, components)
                updater._health_transaction(transaction)
                transaction["phase"] = "end_pending"
                _write_private_json(updater, journal_path, transaction)
        except Exception as error:
            try:
                _stop_candidate_services(updater, components)
                _remove_candidate_pointers_and_units(updater, components)
            except Exception as cleanup_error:  # noqa: BLE001 - retain the hold if ownership is unclear.
                transaction["cleanupError"] = str(cleanup_error)[:300]
            transaction["failure"] = str(error)[:500]
            transaction["phase"] = "hold_required"
            _write_private_json(updater, journal_path, transaction)
            raise RuntimeError(
                f"First-Core bootstrap failed; durable admission hold remains closed: {error}"
            ) from error

        # ── Phase 2: Revalidate before every idempotent EndMaintenance attempt.
        # 第二阶段：每次解除闭门前重新验证PID、发布字节和真实Kernel计数。
        try:
            _verify_live_core_cohort(updater, transaction, plan)
            _require_core_ready(updater, plan)
            if plan.get("includeProducts") is True:
                product_helper = _first_products_module()
                if transaction.get("phase") == "end_call_pending":
                    product_helper.verify_held(updater, plan, transaction)
                else:
                    product_helper.activate_held(updater, plan, transaction)
                _write_private_json(updater, journal_path, transaction)
                _require_core_ready(updater, plan)
        except Exception as error:
            if transaction["phase"] == "end_call_pending":
                # The previous End RPC may have opened the gate before its
                # response was lost. Never tear down a possibly admitted Core.
                transaction["endRecoveryError"] = str(error)[:500]
                _write_private_json(updater, journal_path, transaction)
                raise RuntimeError(
                    "EndMaintenance outcome is uncertain and live Core proof failed; "
                    "preserve the journal and candidate services for operator recovery"
                ) from error
            try:
                _rollback_pre_end(updater, plan, transaction, components)
            except Exception as cleanup_error:  # noqa: BLE001 - preserve candidate state when ownership is unknown.
                transaction["cleanupError"] = str(cleanup_error)[:300]
            transaction["failure"] = str(error)[:500]
            transaction["phase"] = "hold_required"
            _write_private_json(updater, journal_path, transaction)
            raise RuntimeError(
                f"First-Core health proof failed; durable admission hold remains closed: {error}"
            ) from error
        if transaction["phase"] != "end_call_pending":
            transaction["phase"] = "end_call_pending"
            _write_private_json(updater, journal_path, transaction)

        # ── Phase 3: End the durable hold, then finalize without cleanup-on-error.
        # 第三阶段：End结果不确定时保留活动服务与原journal，以同请求幂等恢复。
        try:
            end_result = updater._broker_request(
                "EndMaintenance",
                {
                    "request_id": plan["requestId"],
                    "target_kind": "CORE_RUNTIME",
                    "maintenance_token": transaction["maintenanceToken"],
                    "outcome": "SUCCESS",
                    "healthy": True,
                },
                request_id="bootstrap-end-" + plan_digest.split(":", 1)[1][:32],
            )
        except Exception as error:
            transaction["endError"] = str(error)[:300]
            _write_private_json(updater, journal_path, transaction)
            raise RuntimeError(
                f"EndMaintenance result is uncertain; preserve the first-Core journal and retry this exact plan: {error}"
            ) from error
        if end_result.get("unlocked") is not True:
            transaction["endError"] = "Platform did not confirm the durable gate release"
            _write_private_json(updater, journal_path, transaction)
            raise RuntimeError(
                "Platform did not confirm gate release; preserve the first-Core journal and retry this exact plan"
            )

        transaction["phase"] = (
            "post_end_pending" if plan.get("includeProducts") is True else "end_confirmed"
        )
        transaction["gateReleaseConfirmed"] = True
        transaction.pop("maintenanceToken", None)
        if plan.get("includeProducts") is True:
            transaction["firstProducts"]["phase"] = "post_end_pending"
        _write_private_json(updater, journal_path, transaction)
        if plan.get("includeProducts") is True:
            return _resume_first_products_post_end(
                updater, plan, transaction, journal_path, recovery=False
            )
        try:
            post_end = updater._readiness_for("CORE_RUNTIME", requires_restart=True, force=True)
            transaction["postEndReadiness"] = post_end.get("status", "UNKNOWN")
        except Exception as error:  # noqa: BLE001 - release is confirmed; report unavailable post-release status.
            transaction["postEndReadiness"] = "UNKNOWN"
            transaction["postEndReadinessError"] = str(error)[:300]
        transaction["phase"] = "succeeded"
        _write_private_json(updater, journal_path, transaction)
        return {
            "status": "installed",
            "planId": plan_id,
            "planDigest": plan_digest,
            "components": list(CORE_COMPONENT_IDS),
            "postEndReadiness": transaction["postEndReadiness"],
        }


def handle(updater: Any, request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle an explicitly selected first-Core mode within the four fixed operations."""

    if "bootstrapMode" in request and request.get("bootstrapMode") != CORE_BOOTSTRAP_MODE:
        raise ValueError("Unsupported bootstrap mode")
    if request.get("bootstrapMode") != CORE_BOOTSTRAP_MODE:
        return None
    operation = request.get("operation")
    if operation == "status":
        if set(request) - {
            "protocolVersion",
            "operation",
            "bootstrapMode",
            "includeProducts",
        }:
            raise ValueError("Unsupported first-Core status field")
        include_products = request.get("includeProducts", False)
        if not isinstance(include_products, bool):
            raise TypeError("includeProducts must be a boolean")
        result = updater.status()
        try:
            _assert_fresh(updater)
            snapshot = _health_snapshot(updater, require_eligible=False)
            eligibility = {
                "status": "eligible" if snapshot["coreBootstrapEligible"] else "blocked",
                "blocker_codes": []
                if snapshot["coreBootstrapEligible"]
                else ["FIRST_CORE_ALREADY_USED"],
                "gateGeneration": snapshot["gateGeneration"],
                "catalogGeneration": snapshot["catalogGeneration"],
                "activitySources": snapshot["activitySources"],
            }
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            eligibility = {
                "status": "UNKNOWN",
                "blocker_codes": ["FIRST_CORE_NOT_FRESH"],
                "message": str(error)[:300],
            }
        result["mode"] = CORE_BOOTSTRAP_MODE
        result["includeProducts"] = include_products
        result["firstCoreEligibility"] = eligibility
        transaction = _read_private_json(_journal_path(updater))
        if transaction is not None and transaction.get("includeProducts") is True:
            phase = transaction.get("phase")
            product_status = (
                "complete"
                if phase == "succeeded"
                else "UNKNOWN"
                if phase == "end_call_pending"
                else "pending"
            )
            result["firstProductsBootstrap"] = {
                "status": product_status,
                "phase": phase,
                "gateReleaseConfirmed": transaction.get("gateReleaseConfirmed") is True,
            }
        return result
    if operation == "check":
        if set(request) - {
            "protocolVersion",
            "operation",
            "bootstrapMode",
            "channel",
            "includeProducts",
        }:
            raise ValueError("Unsupported first-Core check field")
        include_products = request.get("includeProducts", False)
        if not isinstance(include_products, bool):
            raise TypeError("includeProducts must be a boolean")
        return check(
            updater,
            channel=request.get("channel"),
            include_products=include_products,
        )
    if operation == "stage":
        if set(request) - {
            "protocolVersion",
            "operation",
            "bootstrapMode",
            "planId",
            "planDigest",
            "channel",
        }:
            raise ValueError("Unsupported first-Core stage field")
        return stage(
            updater,
            request.get("planId"),
            request.get("planDigest"),
            channel=request.get("channel"),
        )
    if operation == "apply":
        if set(request) - {
            "protocolVersion",
            "operation",
            "bootstrapMode",
            "planId",
            "planDigest",
            "confirmation",
            "channel",
        }:
            raise ValueError("Unsupported first-Core apply field")
        return apply(
            updater,
            request.get("planId"),
            request.get("planDigest"),
            request.get("confirmation"),
            channel=request.get("channel"),
        )
    return None
