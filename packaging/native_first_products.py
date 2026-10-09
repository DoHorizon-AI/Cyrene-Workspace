"""First Product cohort activation and trainer runtime preparation.

This helper consumes only the fixed, root-written native initialization receipt
and the five Product bundles already staged by the Debian package. It keeps
their first activation inside the first-Core maintenance hold, then treats
Yield trainer preparation as a separate retryable post-End phase.
中文：仅基于固定安装回执激活首批 Product；Yield 训练运行时在 End 后单独准备。
"""

from __future__ import annotations

import grp
import hashlib
import http.client
import json
import os
import pwd
import re
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ROOT_UID = 0
RECEIPT_PATH = Path("/var/lib/cyrene/native-initialization/first-product-cohort.json")
RECEIPT_PARENT = RECEIPT_PATH.parent
PRODUCTS = ("navigator", "yield", "reactor", "exchange", "catalyst")
COMPONENT_IDS = {
    "navigator": "cyrene-navigator",
    "yield": "cyrene-yield",
    "reactor": "cyrene-reactor",
    "exchange": "cyrene-exchange",
    "catalyst": "cyrene-catalyst",
}
PORT_ENV = {
    "navigator": "CYRENE_PORT_NAVIGATOR",
    "yield": "CYRENE_PORT_YIELD",
    "reactor": "CYRENE_PORT_REACTOR",
    "exchange": "CYRENE_PORT_EXCHANGE",
    "catalyst": "CYRENE_PORT_CATALYST",
}
DEFAULT_PORTS = {
    "navigator": 7860,
    "yield": 8001,
    "reactor": 8002,
    "exchange": 8003,
    "catalyst": 8004,
}
RECEIPT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
PROC_ROOT = Path("/proc")
CYRENE_ENV = Path("/etc/cyrene/cyrene.env")
SERVICE_INDEX = Path("/usr/share/cyrene/service-artifacts/index.json")
INSTALL_CONTRACT = Path("/usr/share/cyrene/native-install-contract-v1.json")
PLATFORM_RUNTIME_MANIFEST = Path("/etc/cyrene/runtime/platform.json")
MANAGED_RUNTIME_HELPER = Path("share/cyrene-managed-runtime/cyrene_managed_runtime.py")
PLATFORM_RUNTIME_PROFILE = "CYRENE_PLATFORM_RUNTIME_V1_LOCAL_GPU"
HEARTBEAT_TIMEOUT_SECONDS = 30
HEARTBEAT_POLL_SECONDS = 1
PHASES = {
    "planned",
    "staging",
    "staged",
    "activating",
    "active",
    "post_end_pending",
    "preparing",
    "prepared",
    "yield_restart_pending",
    "complete",
}


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> bytes:
    """Encode the receipt's JSON-only identity using its trusted JCS subset."""

    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, list):
        return b"[" + b",".join(_canonical(item) for item in value) + b"]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda item: item.encode("utf-16-be"))
        return (
            b"{" + b",".join(_canonical(key) + b":" + _canonical(value[key]) for key in keys) + b"}"
        )
    raise TypeError("First-Product receipt contains a non-JCS value")


def _read_receipt(path: Path = RECEIPT_PATH) -> dict[str, Any]:
    """Read a single root-private receipt and reject alternate paths or metadata."""

    if path != RECEIPT_PATH:
        raise ValueError("First-Product receipt path is fixed by the native installer")
    parent = RECEIPT_PARENT
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("First-Product receipt directory is missing or unsafe")
    parent_info = parent.lstat()
    if parent_info.st_uid != ROOT_UID or stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise ValueError("First-Product receipt directory must be root-owned mode 0700")
    if path.is_symlink() or not path.is_file():
        raise ValueError("First-Product receipt is missing or unsafe")
    info = path.lstat()
    if info.st_uid != ROOT_UID or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("First-Product receipt must be root-owned mode 0600")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("First-Product receipt is not valid UTF-8 JSON") from error
    if not isinstance(receipt, dict):
        raise TypeError("First-Product receipt must be an object")
    required = {"schemaVersion", "kind", "installer", "products", "receiptDigest"}
    if set(receipt) != required or receipt.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("First-Product receipt schema is unsupported")
    if receipt.get("kind") != "first-product-cohort":
        raise ValueError("First-Product receipt kind is invalid")
    installer = receipt.get("installer")
    if not isinstance(installer, dict) or set(installer) != {
        "debSha256",
        "sourceCommit",
        "targetId",
    }:
        raise ValueError("First-Product installer identity is incomplete")
    if (
        not isinstance(installer.get("debSha256"), str)
        or not RECEIPT_DIGEST.fullmatch(installer["debSha256"])
        or not isinstance(installer.get("sourceCommit"), str)
        or not COMMIT.fullmatch(installer["sourceCommit"])
        or not isinstance(installer.get("targetId"), str)
        or not installer["targetId"].startswith("linux-ubuntu-")
    ):
        raise ValueError("First-Product installer identity is malformed")
    products = receipt.get("products")
    if not isinstance(products, list) or [
        row.get("service") if isinstance(row, dict) else None for row in products
    ] != list(PRODUCTS):
        raise ValueError("First-Product receipt must contain the exact ordered five-service cohort")
    for row in products:
        _validate_receipt_row(row)
    material = {key: value for key, value in receipt.items() if key != "receiptDigest"}
    if receipt.get("receiptDigest") != _digest(_canonical(material)):
        raise ValueError("First-Product receipt digest does not match its canonical contents")
    return receipt


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate receipt field: {key}")
        result[key] = value
    return result


def _validate_receipt_row(row: Any) -> None:
    if not isinstance(row, dict) or set(row) != {
        "service",
        "componentId",
        "version",
        "manifestDigest",
        "artifactDigest",
        "bundlePath",
    }:
        raise ValueError("First-Product bundle identity has an invalid shape")
    service = row.get("service")
    version = row.get("version")
    if (
        service not in PRODUCTS
        or row.get("componentId") != COMPONENT_IDS[service]
        or not isinstance(version, str)
        or not VERSION.fullmatch(version)
        or row.get("bundlePath") != f"/usr/share/cyrene/service-artifacts/{service}/{version}"
        or not isinstance(row.get("manifestDigest"), str)
        or not RECEIPT_DIGEST.fullmatch(row["manifestDigest"])
        or not isinstance(row.get("artifactDigest"), str)
        or not RECEIPT_DIGEST.fullmatch(row["artifactDigest"])
    ):
        raise ValueError(
            "First-Product bundle identity is malformed or points outside the fixed DEB tree"
        )


def _bundle_module(updater: Any) -> Any:
    return updater._load_service_bundle()


def _typed_bundle_artifact_digest(value: Any) -> str | None:
    """Convert a raw bundle digest to the receipt's typed SHA-256 form.

    中文：仅将 bundle manifest 的裸 SHA-256 hex 转为 receipt 类型化格式。
    """

    if not isinstance(value, str) or RAW_SHA256.fullmatch(value) is None:
        return None
    return f"sha256:{value}"


def _verified_products(
    updater: Any, core_plan: dict[str, Any], path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Revalidate the signed DEB cohort against the actual immutable bundle bytes."""

    receipt = _read_receipt(path)
    core_target = core_plan.get("targetId")
    if core_target is None:
        core_target = next(
            (
                item.get("targetId")
                for item in core_plan.get("components", [])
                if isinstance(item, dict)
            ),
            None,
        )
    if receipt["installer"]["targetId"] != core_target:
        raise ValueError("Product DEB target differs from the confirmed Core target")
    profile_id = _python_profile_for_core_target(core_target)
    index_bytes = _read_root_file(SERVICE_INDEX)
    contract_bytes = _read_root_file(INSTALL_CONTRACT)
    index = json.loads(index_bytes.decode("utf-8"))
    contract = json.loads(contract_bytes.decode("utf-8"))
    if (
        not isinstance(index, dict)
        or not isinstance(contract, dict)
        or contract.get("targetProfile") != profile_id
        or contract.get("serviceArtifactsMode") != "verified-published-bytes"
        or contract.get("serviceActivation") != "deferred"
        or contract.get("serviceArtifactsIndexSha256") != hashlib.sha256(index_bytes).hexdigest()
        or index.get("schemaVersion") != 1
        or index.get("targetProfile") != profile_id
    ):
        raise ValueError("Installed verified-Product DEB contract or service index changed")
    contract_services = contract.get("services")
    index_services = index.get("services")
    if not isinstance(contract_services, dict) or not isinstance(index_services, dict):
        raise TypeError("Installed service index has no verified source records")
    bundle = _bundle_module(updater)
    verified: list[dict[str, Any]] = []
    for row in receipt["products"]:
        service = row["service"]
        component = updater.components.get(row["componentId"])
        if (
            not isinstance(component, dict)
            or component.get("pythonBundleService") != service
            or component.get("kind") != "python-bundle"
        ):
            raise ValueError(f"Trusted catalog does not own the expected {service} component")
        expected_source = contract_services.get(row["componentId"])
        indexed_source = index_services.get(row["componentId"])
        if (
            not isinstance(expected_source, dict)
            or not isinstance(indexed_source, dict)
            or expected_source.get("source") != indexed_source.get("source")
            or expected_source.get("componentId") != row["componentId"]
        ):
            raise ValueError(f"Installed DEB contract does not bind {service} source identity")
        source = Path(row["bundlePath"])
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"Verified DEB bundle is missing or unsafe for {service}")
        manifest_path = source / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError(f"Verified DEB manifest is missing or unsafe for {service}")
        manifest_bytes = manifest_path.read_bytes()
        product_profile_id = next(
            (
                item.get("targetId")
                for item in component.get("targets", [])
                if isinstance(item, dict)
                and item.get("artifactKind") == "python-bundle"
                and item.get("support") == "supported"
                and item.get("targetId") == profile_id
            ),
            None,
        )
        if product_profile_id != profile_id:
            raise ValueError(f"No trusted service-bundle profile is catalogued for {service}")
        manifest = bundle.validate_bundle(
            source,
            expected_service=service,
            expected_target_profile=product_profile_id,
            release_lock_path=updater.release_lock_path,
        )
        if (
            _digest(manifest_bytes) != row["manifestDigest"]
            or manifest.get("version") != row["version"]
            or _typed_bundle_artifact_digest(manifest.get("artifact_digest"))
            != row["artifactDigest"]
            or manifest.get("source_commit") != expected_source.get("source", {}).get("commit")
            or manifest.get("target", {}).get("profile_id") != product_profile_id
            or manifest.get("schema_version") != 2
        ):
            raise ValueError(f"Verified Product bundle identity changed for {service}")
        verified.append(
            {
                "service": service,
                "componentId": row["componentId"],
                "version": row["version"],
                "manifestDigest": row["manifestDigest"],
                "artifactDigest": row["artifactDigest"],
                "sourceCommit": manifest["source_commit"],
                "targetProfileId": product_profile_id,
                "bundlePath": row["bundlePath"],
                "manifest": manifest,
            }
        )
    return receipt, verified


def _python_profile_for_core_target(core_target: Any) -> str:
    if not isinstance(core_target, str):
        raise TypeError("Confirmed Core plan has no native target identity")
    match = re.fullmatch(r"linux-ubuntu-(22\.04|24\.04)-x86_64-systemd", core_target)
    if match is None:
        raise ValueError("Confirmed Core plan has an unsupported native target")
    return f"linux-ubuntu-{match.group(1)}-x86_64-python-3.12"


def _read_root_file(path: Path) -> bytes:
    """Read a fixed root-owned package evidence file without following symlinks."""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Installed DEB evidence file is missing or unsafe: {path.name}")
    info = path.lstat()
    if info.st_uid != ROOT_UID or info.st_mode & 0o022:
        raise ValueError(f"Installed DEB evidence file is not root-controlled: {path.name}")
    return path.read_bytes()


def _unit_path(
    updater: Any,
    component: dict[str, Any],
    service: str,
    *,
    target_profile: str,
) -> tuple[Path, bytes]:
    """Read a Product unit only through the updater's DEB-owned unit contract."""

    if component.get("pythonBundleService") != service:
        raise TypeError(f"Catalog does not bind the Product unit to {service}")
    validate_unit = getattr(updater, "_workload_deb_managed_unit_bytes", None)
    if not callable(validate_unit):
        raise TypeError("Native updater has no DEB-managed Product unit verifier")
    content, path, _digest = validate_unit(
        component,
        {"targetId": target_profile},
        verify_fragment=True,
    )
    if not isinstance(content, bytes) or not isinstance(path, Path):
        raise TypeError(f"DEB-managed Product unit proof is malformed for {service}")
    return path, content


def _pointer(updater: Any, service: str) -> str | None:
    root = updater.install_root / "services" / service
    active = root / "active"
    if not active.exists() and not active.is_symlink():
        return None
    if active.is_symlink() is False:
        raise ValueError(f"Product active release pointer is unsafe: {service}")
    target = os.readlink(active)
    match = re.fullmatch(r"releases/([^/]+)", target)
    if match is None:
        raise ValueError(f"Product active release pointer has an unsafe target: {service}")
    return match.group(1)


def _expected_pointer(product: dict[str, Any]) -> str:
    return product["version"]


def _check_fresh(
    updater: Any, products: list[dict[str, Any]], *, allow_owned: set[str] = frozenset()
) -> None:
    """Reject pre-existing Product pointers, units, or processes outside this cohort."""

    for product in products:
        service = product["service"]
        current = _pointer(updater, service)
        if current is not None and not (
            service in allow_owned and current == _expected_pointer(product)
        ):
            raise ValueError(
                f"An existing active Product pointer blocks first activation: {service}"
            )
        component = updater.components[product["componentId"]]
        _unit_path(
            updater,
            component,
            service,
            target_profile=product["targetProfileId"],
        )
        pid = _main_pid(updater, component["systemdUnit"])
        if pid != 0 and service not in allow_owned:
            raise ValueError(f"An existing Product process blocks first activation: {service}")
    if "yield" in PRODUCTS and "yield" not in allow_owned:
        yield_product = next(item for item in products if item["service"] == "yield")
        if _yield_runtime_state(updater, yield_product):
            raise ValueError("Existing Yield execution runtime state blocks first activation")


def _yield_runtime_state(updater: Any, product: dict[str, Any]) -> bool:
    """Detect prior trainer configuration that would bypass API-only startup."""

    configured = {name for name, _, _ in _configured_values(CYRENE_ENV)}
    if configured & {"CYRENE_PLATFORM_RUNTIME_CONFIG", "CYRENE_TRAINER_RUNTIME_CONFIG"}:
        return True
    for path in (
        Path("/etc/cyrene/runtime/platform.json"),
        Path("/var/lib/cyrene/yield/runtime.json"),
    ):
        if path.exists() or path.is_symlink():
            return True
    descriptor = product.get("manifest", {}).get("execution_runtime")
    if not isinstance(descriptor, dict):
        return False
    relative = descriptor.get("runtime_home_relative_path")
    if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
        raise ValueError("Signed Yield runtime home path is unsafe")
    data_root = Path(os.environ.get("CYRENE_DATA_DIR", "/var/lib/cyrene"))
    runtime_home = data_root.joinpath(*Path(relative).parts, product["version"])
    return runtime_home.exists() or runtime_home.is_symlink()


def _configured_values(path: Path) -> list[tuple[str, str, str]]:
    """Read fixed package environment assignments without evaluating shell text."""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Cyrene Product configuration is unsafe")
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator:
            result.append((name, value, line))
    return result


def _main_pid(updater: Any, unit: str) -> int:
    result = updater.runner(
        ["systemctl", "show", "--property=MainPID", "--value", unit],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value.isdecimal():
        raise RuntimeError(f"Cannot determine Product unit MainPID for {unit}")
    return int(value)


def _pid_uid(pid: int, proc_root: Path = PROC_ROOT) -> int:
    status = (proc_root / str(pid) / "status").read_text(encoding="ascii")
    match = re.search(r"^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$", status, re.MULTILINE)
    if match is None or len(set(match.groups())) != 1:
        raise RuntimeError(
            "Product process effective and real UID identities differ or are unknown"
        )
    return int(match.group(1))


def _env_ports() -> dict[str, int]:
    result = dict(DEFAULT_PORTS)
    for name, raw, _line in _configured_values(CYRENE_ENV):
        if name in PORT_ENV.values():
            if not raw.isdecimal() or not 1 <= int(raw) <= 65535:
                raise ValueError(f"Invalid fixed Product API port assignment: {name}")
            service = next(key for key, env_name in PORT_ENV.items() if env_name == name)
            result[service] = int(raw)
    return result


def _probe_api(product: dict[str, Any], port: int) -> None:
    """Require the signed bundle health endpoint to answer locally with HTTP 2xx."""

    path = product["manifest"].get("health", {}).get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"Signed Product health path is invalid: {product['service']}")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"User-Agent": "cyrene-native-initializer"}
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            if (
                not 200 <= response.status < 300
                or response.geturl().split("/", 3)[2] != f"127.0.0.1:{port}"
            ):
                raise RuntimeError(
                    f"Product API health endpoint is not locally ready: {product['service']}"
                )
    except (OSError, urllib.error.URLError, TimeoutError, http.client.HTTPException) as error:
        raise RuntimeError(f"Product API is not ready: {product['service']}") from error


def _assert_zero_runtime(
    updater: Any, plan: dict[str, Any], *, allow_maintenance: bool
) -> dict[str, Any]:
    catalog, sources = updater._activity_catalog()
    if catalog.get("generation") != plan.get("catalogGeneration") or sources != plan.get(
        "activitySources"
    ):
        raise RuntimeError(
            "Runtime activity source catalog changed during first Product activation"
        )
    readiness = updater._readiness_for("CORE_RUNTIME", requires_restart=True, force=True)
    if readiness.get("status") not in (
        {"MAINTENANCE_ACTIVE", "READY"} if allow_maintenance else {"READY"}
    ):
        raise RuntimeError("Runtime broker has no strict READY proof for the Product phase")
    if readiness.get("unknown_activity_sources") != []:
        raise RuntimeError("Runtime broker reports unknown Product activity sources")
    if readiness.get("active_tasks") != []:
        raise RuntimeError("Runtime broker reports active Product tasks")
    for key in (
        "active_task_count",
        "inflight_runtime_admission_count",
        "active_worker_count",
        "active_allocation_count",
    ):
        if type(readiness.get(key)) is not int or readiness[key] != 0:
            raise RuntimeError(f"Runtime broker does not report a known zero {key}")
    return readiness


def _live_product(updater: Any, product: dict[str, Any], *, proc_root: Path = PROC_ROOT) -> int:
    component = updater.components[product["componentId"]]
    expected = _expected_pointer(product)
    if _pointer(updater, product["service"]) != expected:
        raise RuntimeError(
            f"Active Product pointer differs from the confirmed cohort: {product['service']}"
        )
    _unit_path(
        updater,
        component,
        product["service"],
        target_profile=product["targetProfileId"],
    )
    pid = _main_pid(updater, component["systemdUnit"])
    if pid <= 1:
        raise RuntimeError(f"Product unit has no live MainPID: {product['service']}")
    try:
        uid = pwd.getpwnam("cyrene").pw_uid
    except KeyError as error:
        raise RuntimeError("The cyrene service account is missing") from error
    if _pid_uid(pid, proc_root) != uid:
        raise RuntimeError(f"Product MainPID does not run as cyrene: {product['service']}")
    command = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    command_text = " ".join(os.fsdecode(part) for part in command if part)
    if f"service-run {product['service']}" not in command_text:
        raise RuntimeError(
            f"Product MainPID is not the fixed API-only service runner: {product['service']}"
        )
    release = (
        updater.install_root / "services" / product["service"] / "releases" / product["version"]
    )
    bundle = _bundle_module(updater)
    installed_manifest = bundle.validate_bundle(
        release,
        expected_service=product["service"],
        expected_target_profile=product["targetProfileId"],
        release_lock_path=updater.release_lock_path,
    )
    if (
        _digest((release / "manifest.json").read_bytes()) != product["manifestDigest"]
        or _typed_bundle_artifact_digest(installed_manifest.get("artifact_digest"))
        != product["artifactDigest"]
    ):
        raise RuntimeError(
            f"Active Product bytes differ from the confirmed cohort: {product['service']}"
        )
    _probe_api(product, _env_ports()[product["service"]])
    return pid


def _plan_products(updater: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Reload manifests from the fixed signed DEB tree instead of trusting plan JSON."""

    block = plan.get("firstProducts")
    if not isinstance(block, dict) or not isinstance(block.get("installer"), dict):
        raise TypeError("Confirmed plan has no immutable First-Product installer identity")
    receipt, actual = _verified_products(
        updater, {"targetId": block["installer"].get("targetId")}, RECEIPT_PATH
    )
    if block != _identity_block(receipt, actual):
        raise ValueError(
            "Confirmed Product plan differs from the current root receipt and bundle bytes"
        )
    return actual


def _identity_block(receipt: dict[str, Any], products: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "receiptDigest": receipt["receiptDigest"],
        "installer": dict(receipt["installer"]),
        "products": [
            {
                key: product[key]
                for key in (
                    "service",
                    "componentId",
                    "version",
                    "manifestDigest",
                    "artifactDigest",
                    "sourceCommit",
                    "targetProfileId",
                    "bundlePath",
                )
            }
            for product in products
        ],
    }


def check(
    updater: Any, core_plan: dict[str, Any], *, receipt_path: Path = RECEIPT_PATH
) -> dict[str, Any]:
    """Return fully validated five-Product identity material for an opt-in plan."""

    receipt, products = _verified_products(updater, core_plan, receipt_path)
    _check_fresh(updater, products)
    return _identity_block(receipt, products)


def stage(updater: Any, core_plan: dict[str, Any], addon_plan: dict[str, Any]) -> dict[str, Any]:
    """Stage the exact five receipt-bound bundles without changing active pointers."""

    receipt, products = _verified_products(updater, core_plan, RECEIPT_PATH)
    expected = _identity_block(receipt, products)
    if addon_plan != expected:
        raise ValueError("First-Product plan identity differs from the root receipt")
    bundle = _bundle_module(updater)
    for product in products:
        source = Path(product["bundlePath"])
        staged = bundle.stage_release(
            source, install_root=updater.install_root, release_lock_path=updater.release_lock_path
        )
        if staged.name != product["version"]:
            raise RuntimeError("Service bundle store staged an unexpected Product release")
    _check_fresh(updater, products)
    return {
        "status": "staged",
        "receiptDigest": receipt["receiptDigest"],
        "products": expected["products"],
    }


def _record(transaction: dict[str, Any]) -> dict[str, Any]:
    record = transaction.get("firstProducts")
    if not isinstance(record, dict) or record.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("First-Product transaction journal is missing its immutable identity")
    if record.get("phase") not in PHASES or not isinstance(record.get("products"), list):
        raise ValueError("First-Product transaction journal is malformed")
    return record


def _product_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    block = plan.get("firstProducts")
    rows = block.get("products") if isinstance(block, dict) else None
    if not isinstance(rows, list) or [
        row.get("service") for row in rows if isinstance(row, dict)
    ] != list(PRODUCTS):
        raise ValueError("Confirmed plan has no exact five-Product identity cohort")
    return rows


def _persist_transaction(updater: Any, transaction: dict[str, Any]) -> None:
    """Atomically persist addon phase and ownership in the enclosing Core journal."""

    directory = Path(updater.state_root) / "native-first-bootstrap"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("First-Product journal directory is unsafe")
    updater._atomic_json_file(directory / "first-core-bootstrap.json", transaction, mode=0o600)


def _write_unit_and_pointer(updater: Any, product: dict[str, Any], record: dict[str, Any]) -> None:
    service = product["service"]
    current = _pointer(updater, service)
    if current is not None and current != _expected_pointer(product):
        raise ValueError(f"Product pointer changed outside the first-cohort journal: {service}")
    if current is None:
        _bundle_module(updater).activate_release(
            service,
            product["version"],
            install_root=updater.install_root,
            expected_current_version=None,
        )
        record.setdefault("ownedPointers", []).append(service)
    else:
        if (
            service not in record.get("ownedPointers", [])
            and record.get("pendingPointer") != service
        ):
            raise ValueError(f"Unjournaled Product pointer blocks activation recovery: {service}")
        if service not in record.setdefault("ownedPointers", []):
            record["ownedPointers"].append(service)


def activate_held(
    updater: Any, bootstrap_plan: dict[str, Any], transaction: dict[str, Any]
) -> dict[str, Any]:
    """Activate and prove all Products while first-Core admission remains held."""

    record = _record(transaction)
    rows = _product_rows(bootstrap_plan)
    actual = _plan_products(updater, bootstrap_plan)
    if (
        record.get("receiptDigest") != bootstrap_plan["firstProducts"].get("receiptDigest")
        or record.get("products") != rows
    ):
        raise ValueError("First-Product transaction identity differs from the confirmed plan")
    if not isinstance(transaction.get("maintenanceToken"), str):
        raise PermissionError("First-Product activation requires the durable Core maintenance hold")
    _assert_core_started(updater, transaction)
    held = updater._readiness_for("CORE_RUNTIME", requires_restart=True, force=True)
    if held.get("status") != "MAINTENANCE_ACTIVE":
        raise RuntimeError("The durable Core maintenance hold is not active")
    owned = set(record.get("ownedPointers", []))
    if record.get("pendingPointer") in PRODUCTS:
        owned.add(record["pendingPointer"])
    if record.get("pendingUnit") in PRODUCTS:
        owned.add(record["pendingUnit"])
    _check_fresh(updater, actual, allow_owned=owned)
    try:
        record["phase"] = "activating"
        _persist_transaction(updater, transaction)
        for product in actual:
            record["pendingPointer"] = product["service"]
            _persist_transaction(updater, transaction)
            _write_unit_and_pointer(updater, product, record)
            record.pop("pendingPointer", None)
            _persist_transaction(updater, transaction)
        updater.runner(
            ["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=30, check=True
        )
        for product in actual:
            component = updater.components[product["componentId"]]
            service = product["service"]
            if _main_pid(updater, component["systemdUnit"]) == 0:
                record["pendingUnit"] = product["service"]
                _persist_transaction(updater, transaction)
                updater._run_systemctl("start", component["systemdUnit"])
                record.setdefault("ownedUnits", []).append(product["service"])
                record.pop("pendingUnit", None)
                _persist_transaction(updater, transaction)
            elif service == record.get("pendingUnit"):
                record.setdefault("ownedUnits", []).append(service)
                record.pop("pendingUnit", None)
                _persist_transaction(updater, transaction)
            updater._wait_unit_active(component["systemdUnit"])
        record["phase"] = "active"
        proof = verify_held(updater, bootstrap_plan, transaction)
        record["ownedPids"] = proof["pids"]
        _persist_transaction(updater, transaction)
        return {"status": "active", "products": list(PRODUCTS)}
    except Exception:
        rollback_pre_end(updater, bootstrap_plan, transaction)
        raise


def verify_held(
    updater: Any, bootstrap_plan: dict[str, Any], transaction: dict[str, Any]
) -> dict[str, Any]:
    """Read-only proof of exact staged Products, API processes, and empty broker state."""

    record = _record(transaction)
    rows = _product_rows(bootstrap_plan)
    actual = _plan_products(updater, bootstrap_plan)
    if record.get("products") != rows:
        raise ValueError("First-Product journal no longer matches its confirmed plan")
    deadline = time.monotonic() + HEARTBEAT_TIMEOUT_SECONDS
    while True:
        try:
            pids = [_live_product(updater, product) for product in actual]
            readiness = _assert_zero_runtime(updater, bootstrap_plan, allow_maintenance=True)
            break
        except Exception as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Product API/source heartbeat proof remained incomplete: {error}"
                ) from error
            time.sleep(HEARTBEAT_POLL_SECONDS)
    return {"status": "verified", "pids": pids, "readiness": readiness.get("status")}


def _assert_core_started(updater: Any, transaction: dict[str, Any]) -> None:
    """Require all four confirmed Core pointers and live units before Product start."""

    expected = {
        "cyrene-linux-sys-adapter",
        "cyrene-nvidia-adapter",
        "cyrene-sandboxd",
        "cyrene-kernel",
    }
    components = transaction.get("components")
    if (
        not isinstance(components, list)
        or {item.get("componentId") for item in components if isinstance(item, dict)} != expected
    ):
        raise RuntimeError("First-Product activation requires the exact four-component Core cohort")
    for item in components:
        component_id = item["componentId"]
        pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
        if updater._active_native_pointer_identity(component_id) != pointer:
            raise RuntimeError("First-Product activation requires all confirmed Core pointers")
        component = updater.components[component_id]
        if _main_pid(updater, component["systemdUnit"]) <= 1:
            raise RuntimeError("First-Product activation requires all four live Core units")


def rollback_pre_end(
    updater: Any, bootstrap_plan: dict[str, Any], transaction: dict[str, Any]
) -> None:
    """Remove only journal-owned new Product processes and pointers before End."""

    record = _record(transaction)
    if transaction.get("phase") in {"end_call_pending", "end_confirmed", "succeeded"}:
        raise ValueError("End outcome may have opened admission; Product cleanup is forbidden")
    if record.get("phase") in {
        "post_end_pending",
        "preparing",
        "prepared",
        "yield_restart_pending",
        "complete",
    }:
        raise ValueError("Post-End Product state is durable and cannot be rolled back")
    rows = _plan_products(updater, bootstrap_plan)
    owned = set(record.get("ownedPointers", []))
    if record.get("pendingPointer") in PRODUCTS:
        owned.add(record["pendingPointer"])
    owned_units = set(record.get("ownedUnits", []))
    if record.get("pendingUnit") in PRODUCTS:
        owned_units.add(record["pendingUnit"])
    # Prove every active pointer and PID before stopping any unit.
    live: list[tuple[dict[str, Any], int]] = []
    for product in reversed(rows):
        if product["service"] not in owned:
            continue
        if _pointer(updater, product["service"]) != _expected_pointer(product):
            raise RuntimeError("Cannot prove an exact addon-owned Product pointer for cleanup")
        pid = _main_pid(updater, updater.components[product["componentId"]]["systemdUnit"])
        if pid > 1 and product["service"] in owned_units | owned:
            if _pid_uid(pid) != pwd.getpwnam("cyrene").pw_uid:
                raise RuntimeError("Cannot prove ownership of a Product PID; leave the hold intact")
            live.append((product, pid))
    for product, _ in live:
        unit = updater.components[product["componentId"]]["systemdUnit"]
        updater._run_systemctl("stop", unit)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _main_pid(updater, unit) != 0:
            time.sleep(0.1)
        if _main_pid(updater, unit) != 0:
            raise RuntimeError("Journal-owned Product process did not stop; keep the Core hold")
    for product in reversed(rows):
        service = product["service"]
        if service in owned and _pointer(updater, service) == _expected_pointer(product):
            _remove_active_pointer(updater, service, product)
    record["ownedPointers"] = []
    record["ownedUnits"] = []
    record["ownedPids"] = []
    record["phase"] = "staged"
    _persist_transaction(updater, transaction)


def _remove_active_pointer(updater: Any, service: str, product: dict[str, Any]) -> None:
    active = updater.install_root / "services" / service / "active"
    if _pointer(updater, service) != _expected_pointer(product):
        raise RuntimeError("Product active pointer changed during owned cleanup")
    active.unlink()
    updater._fsync_directory(active.parent)


def complete_post_end(
    updater: Any, bootstrap_plan: dict[str, Any], transaction: dict[str, Any]
) -> dict[str, Any]:
    """Prepare Yield's signed trainer runtime, then restart only Yield under a fresh hold."""

    return _post_end(updater, bootstrap_plan, transaction)


def recover_post_end(
    updater: Any, bootstrap_plan: dict[str, Any], transaction: dict[str, Any]
) -> dict[str, Any]:
    """Retry the same durable post-End preparation without changing Product pointers."""

    return _post_end(updater, bootstrap_plan, transaction)


def _post_end(updater: Any, plan: dict[str, Any], transaction: dict[str, Any]) -> dict[str, Any]:
    record = _record(transaction)
    rows = _plan_products(updater, plan)
    if record.get("products") != rows or record.get("receiptDigest") != plan["firstProducts"].get(
        "receiptDigest"
    ):
        raise ValueError("Post-End Product journal differs from the confirmed cohort")
    if transaction.get("gateReleaseConfirmed") is not True:
        raise ValueError("Trainer preparation cannot run before confirmed first-Core End")
    yield_product = next(item for item in rows if item["service"] == "yield")
    if record.get("phase") not in {"complete"}:
        try:
            _assert_zero_runtime(updater, plan, allow_maintenance=False)
            if record.get("phase") not in {"prepared", "yield_restart_pending"}:
                _observe_platform_runtime(updater, plan, transaction, record)
            if record.get("phase") not in {"prepared", "yield_restart_pending"}:
                record["phase"] = "preparing"
                _persist_transaction(updater, transaction)
                bundle = _bundle_module(updater)
                result = bundle.prepare_execution_runtime(
                    "yield",
                    version=yield_product["version"],
                    install_root=updater.install_root,
                    release_lock_path=updater.release_lock_path,
                    runtime_lock_path=bundle.DEFAULT_PYTHON_RUNTIME_LOCK,
                    runner=updater.runner,
                )
                if result.get("status") not in {"prepared", "already_prepared"}:
                    raise RuntimeError("Signed Yield runtime preparation did not confirm READY")
                record["phase"] = "prepared"
                _persist_transaction(updater, transaction)
            _fresh_yield_restart(updater, plan, transaction, record, yield_product)
            record["phase"] = "complete"
            _persist_transaction(updater, transaction)
            return {"status": "complete", "phase": "complete"}
        except Exception as error:  # noqa: BLE001 - post-End faults remain retryable and preserve all pointers.
            record["phase"] = "post_end_pending"
            record["lastError"] = str(error)[:300]
            _persist_transaction(updater, transaction)
            return {"status": "pending", "phase": record["phase"], "reason": str(error)[:300]}
    return {"status": "complete", "phase": "complete"}


def _observe_platform_runtime(
    updater: Any, plan: dict[str, Any], transaction: dict[str, Any], record: dict[str, Any]
) -> None:
    """Invoke the signed observer and retain a truthful serving projection."""

    units = _runtime_identity_units(updater, transaction)
    evidence = {"schemaVersion": 1, "units": units}
    state_dir = Path(updater.state_root) / "native-first-bootstrap"
    evidence_path = state_dir / "managed-runtime-identity.json"
    updater._atomic_json_file(evidence_path, evidence, mode=0o600)
    helper = _active_runtime_helper(updater)
    python = Path("/opt/cyrene/python/3.12.14/bin/python3.12")
    if not _root_file_matches(python, executable=True):
        raise RuntimeError("Pinned private Python for signed runtime observation is unavailable")
    if not _root_file_matches(helper):
        raise RuntimeError("Signed managed-runtime observer bytes are unavailable")
    if (PLATFORM_RUNTIME_MANIFEST.exists() or PLATFORM_RUNTIME_MANIFEST.is_symlink()) and (
        record.get("platformManifestDigest") is None
        or PLATFORM_RUNTIME_MANIFEST.is_symlink()
        or not PLATFORM_RUNTIME_MANIFEST.is_file()
        or _digest(PLATFORM_RUNTIME_MANIFEST.read_bytes()) != record.get("platformManifestDigest")
    ):
        raise RuntimeError("An unowned Platform runtime manifest blocks signed observation")
    record["observePhase"] = "observing"
    _persist_transaction(updater, transaction)
    completed = updater.runner(
        [str(python), "-sE", str(helper), "observe", "--identity-json", str(evidence_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        # A failed observer is not permission to rewrite a pre-existing serving
        # projection. The signed helper writes only after successful inspection.
        raise RuntimeError("Signed managed-runtime observer did not complete")
    lines = completed.stdout.splitlines()
    if len(lines) != 1:
        raise RuntimeError("Signed managed-runtime observer returned malformed JSON")
    result = json.loads(lines[0])
    if not isinstance(result, dict) or result.get("status") != "READY":
        raise RuntimeError("Signed managed-runtime observer did not report a serving runtime")
    _verify_platform_projection()
    projection_digest = _digest(PLATFORM_RUNTIME_MANIFEST.read_bytes())
    authority_status = result.get("authorityStatus")
    workers = result.get("activeWorkers")
    allocations = result.get("activeAllocations")
    if authority_status not in {"READY", "ACTIVE_TASKS"}:
        raise RuntimeError("Signed observer returned unknown broker authority status")
    if type(workers) is not int or workers < 0 or type(allocations) is not int or allocations < 0:
        raise RuntimeError("Signed observer returned unknown broker activity counts")
    record["platformManifestDigest"] = projection_digest
    record["observeAuthorityStatus"] = authority_status
    record["observeActiveWorkers"] = workers
    record["observeActiveAllocations"] = allocations
    record.pop("observePhase", None)
    _persist_transaction(updater, transaction)
    if authority_status != "READY" or workers != 0 or allocations != 0:
        # ACTIVE_TASKS is a valid serving projection. Keep it intact, but leave
        # trainer preparation and the overall first-init transaction pending.
        raise RuntimeError("Signed runtime is serving while broker activity remains active")
    _assert_zero_runtime(updater, plan, allow_maintenance=False)


def _active_runtime_helper(updater: Any) -> Path:
    """Resolve the observer only from the currently active signed broker release."""

    component_id = "cyrene-runtime-maintenance"
    pointer = updater._active_native_pointer_identity(component_id)
    receipt = updater._read_active_receipt(component_id)
    if not _receipt_matches_pointer(component_id, pointer, receipt):
        raise RuntimeError("Active maintenance pointer differs from its verified release receipt")
    manifest = receipt.get("manifest") if isinstance(receipt, dict) else None
    artifact = manifest.get("artifact") if isinstance(manifest, dict) else None
    files = artifact.get("files") if isinstance(artifact, dict) else None
    digest = files.get(MANAGED_RUNTIME_HELPER.as_posix()) if isinstance(files, dict) else None
    if (
        not isinstance(pointer, str)
        or not isinstance(digest, str)
        or not RECEIPT_DIGEST.fullmatch(digest)
    ):
        raise RuntimeError("Active signed maintenance release omits the managed-runtime observer")
    helper = (
        updater.install_root
        / "components"
        / component_id
        / "releases"
        / pointer
        / MANAGED_RUNTIME_HELPER
    )
    if not _root_file_matches(helper) or _digest(helper.read_bytes()) != digest:
        raise RuntimeError("Active signed managed-runtime observer bytes differ from their receipt")
    return helper


def _receipt_matches_pointer(component_id: str, pointer: Any, receipt: Any) -> bool:
    """Bind a live active symlink to the updater-validated receipt identity."""

    if not isinstance(pointer, str) or not isinstance(receipt, dict):
        return False
    version = receipt.get("version")
    manifest_digest = receipt.get("manifestDigest")
    return (
        receipt.get("componentId") == component_id
        and isinstance(version, str)
        and isinstance(manifest_digest, str)
        and RECEIPT_DIGEST.fullmatch(manifest_digest) is not None
        and receipt.get("releaseIdentity") == manifest_digest
        and pointer == f"{version}--{manifest_digest.removeprefix('sha256:')}"
    )


def _runtime_identity_units(updater: Any, transaction: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Build identity evidence only from signed releases and their installed unit bytes."""

    result: dict[str, dict[str, str]] = {}
    core_units = {
        "cyrene-kernel": "cyrene-kernel.service",
        "cyrene-sandboxd": "cyrene-sandboxd.service",
        "cyrene-linux-sys-adapter": "cyrene-linux-sys-adapter.service",
        "cyrene-nvidia-adapter": "cyrene-nvidia-adapter.service",
    }
    core_items = {
        item.get("componentId"): item
        for item in transaction.get("components", [])
        if isinstance(item, dict)
    }
    for component_id, unit in core_units.items():
        item = core_items.get(component_id)
        if not isinstance(item, dict):
            raise TypeError(f"First-Core journal omits signed runtime component {component_id}")
        manifest = item.get("manifest")
        pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
        if updater._active_native_pointer_identity(component_id) != pointer:
            raise RuntimeError(
                f"Signed runtime component is not the active release: {component_id}"
            )
        result[unit] = _unit_binary_identity(updater, component_id, pointer, manifest, unit)
    maintenance_id = "cyrene-runtime-maintenance"
    pointer = updater._active_native_pointer_identity(maintenance_id)
    receipt = updater._read_active_receipt(maintenance_id)
    if not _receipt_matches_pointer(maintenance_id, pointer, receipt):
        raise TypeError("Signed active maintenance broker identity is unavailable")
    result["cyrene-runtime-maintenance.service"] = _unit_binary_identity(
        updater,
        maintenance_id,
        pointer,
        receipt.get("manifest"),
        "cyrene-runtime-maintenance.service",
    )
    if set(result) != {
        "cyrene-kernel.service",
        "cyrene-sandboxd.service",
        "cyrene-linux-sys-adapter.service",
        "cyrene-nvidia-adapter.service",
        "cyrene-runtime-maintenance.service",
    }:
        raise RuntimeError("Signed runtime identity evidence does not cover the exact five units")
    return result


def _unit_binary_identity(
    updater: Any, component_id: str, pointer: str, manifest: Any, unit: str
) -> dict[str, str]:
    artifact = manifest.get("artifact") if isinstance(manifest, dict) else None
    files = artifact.get("files") if isinstance(artifact, dict) else None
    entrypoint = artifact.get("entrypoint") if isinstance(artifact, dict) else None
    unit_relative = f"systemd/{unit}"
    unit_digest = files.get(unit_relative) if isinstance(files, dict) else None
    binary_digest = (
        files.get(entrypoint) if isinstance(files, dict) and isinstance(entrypoint, str) else None
    )
    if (
        not isinstance(unit_digest, str)
        or not isinstance(binary_digest, str)
        or not isinstance(entrypoint, str)
    ):
        raise TypeError(f"Signed runtime release lacks exact unit or binary hashes: {component_id}")
    release = updater.install_root / "components" / component_id / "releases" / pointer
    source_unit = release / unit_relative
    binary = release / entrypoint
    if not _root_file_matches(source_unit) or not _root_file_matches(binary, executable=True):
        raise RuntimeError(f"Signed runtime unit or binary path is unsafe: {component_id}")
    if (
        _digest(source_unit.read_bytes()) != unit_digest
        or _digest(binary.read_bytes()) != binary_digest
    ):
        raise RuntimeError(f"Signed runtime unit or binary digest changed: {component_id}")
    installed_unit = next(
        (
            Path(directory) / unit
            for directory in updater.systemd_unit_dirs
            if (Path(directory) / unit).is_file()
        ),
        None,
    )
    if (
        installed_unit is None
        or installed_unit.is_symlink()
        or installed_unit.read_bytes() != source_unit.read_bytes()
    ):
        raise RuntimeError(f"Installed runtime unit differs from its signed release: {unit}")
    return {
        "unitFile": str(installed_unit),
        "unitSha256": unit_digest.removeprefix("sha256:"),
        "binaryPath": str(binary),
        "binarySha256": binary_digest.removeprefix("sha256:"),
    }


def _root_file_matches(path: Path, *, executable: bool = False) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    info = path.lstat()
    return (
        info.st_uid == ROOT_UID
        and not info.st_mode & 0o022
        and (not executable or os.access(path, os.X_OK))
    )


def _verify_platform_projection() -> None:
    if PLATFORM_RUNTIME_MANIFEST.is_symlink() or not PLATFORM_RUNTIME_MANIFEST.is_file():
        raise RuntimeError("Signed observer did not write the Platform runtime projection")
    info = PLATFORM_RUNTIME_MANIFEST.lstat()
    cyrene_gid = grp.getgrnam("cyrene").gr_gid
    value = json.loads(PLATFORM_RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    expected_units = {"kernel", "sandboxd", "systemAdapter", "nvidiaAdapter"}
    if (
        info.st_uid != ROOT_UID
        or info.st_gid != cyrene_gid
        or stat.S_IMODE(info.st_mode) != 0o640
        or not isinstance(value, dict)
        or value.get("schemaVersion") != 1
        or value.get("profile") != PLATFORM_RUNTIME_PROFILE
        or value.get("status") != "READY"
        or not isinstance(value.get("components"), dict)
        or set(value["components"]) != expected_units
        or any(
            not isinstance(item, dict) or item.get("status") != "READY"
            for item in value["components"].values()
        )
    ):
        raise RuntimeError("Platform runtime projection is incomplete or not strictly READY")


def _fresh_yield_restart(
    updater: Any,
    plan: dict[str, Any],
    transaction: dict[str, Any],
    record: dict[str, Any],
    product: dict[str, Any],
) -> None:
    """Acquire a fresh PACKAGE_ONLY hold, restart Yield, verify, and release it."""

    component = updater.components[product["componentId"]]
    restart = record.get("yieldRestart")
    if restart is None:
        catalog, sources = updater._activity_catalog()
        readiness = updater._readiness_for("PACKAGE_ONLY", requires_restart=True, force=True)
        if readiness.get("status") != "READY" or readiness.get("unknown_activity_sources") != []:
            raise RuntimeError("Yield restart is deferred until a fresh strict READY snapshot")
        for key in (
            "active_task_count",
            "inflight_runtime_admission_count",
            "active_worker_count",
            "active_allocation_count",
        ):
            if type(readiness.get(key)) is not int or readiness[key] != 0:
                raise RuntimeError(
                    "Yield restart is deferred because runtime activity is not known empty"
                )
        material = {
            "schemaVersion": 1,
            "mode": "first-product-yield-restart",
            "parentPlanId": plan["planId"],
            "parentPlanDigest": plan["planDigest"],
            "receiptDigest": plan["firstProducts"]["receiptDigest"],
            "componentArtifactDigests": {product["componentId"]: product["artifactDigest"]},
            "catalogGeneration": catalog["generation"],
            "gateGeneration": readiness["gate_generation"],
            "activitySources": sources,
        }
        digest = _digest(_canonical(material))
        plan_id = "plan-" + digest.split(":", 1)[1][:32]
        restart_tx = {
            "schemaVersion": 2,
            "planId": plan_id,
            "planDigest": digest,
            "requestId": "cyrene-update-" + plan_id,
            "targetKind": "PACKAGE_ONLY",
            "expectedCatalogGeneration": catalog["generation"],
            "expectedGateGeneration": readiness["gate_generation"],
            "expectedActivitySources": sources,
            "componentArtifactDigests": material["componentArtifactDigests"],
            "components": [
                {"componentId": product["componentId"], "artifactDigest": product["artifactDigest"]}
            ],
            "phase": "begin_pending",
        }
        restart = {
            "plan": material | {"planId": plan_id, "planDigest": digest},
            "transaction": restart_tx,
            "phase": "begin_pending",
        }
        record["yieldRestart"] = restart
        record["phase"] = "yield_restart_pending"
        _persist_transaction(updater, transaction)
    restart_tx = restart.get("transaction")
    expected_plan = restart.get("plan")
    if (
        not isinstance(restart_tx, dict)
        or not isinstance(expected_plan, dict)
        or expected_plan.get("parentPlanDigest") != plan["planDigest"]
        or expected_plan.get("receiptDigest") != plan["firstProducts"]["receiptDigest"]
        or restart_tx.get("targetKind") != "PACKAGE_ONLY"
        or restart_tx.get("componentArtifactDigests")
        != {product["componentId"]: product["artifactDigest"]}
    ):
        raise ValueError("Durable Yield restart transaction differs from its parent Product plan")

    # Repeating Begin with the same plan identity recovers a lost response without
    # taking over another maintenance transaction.
    if not isinstance(restart_tx.get("maintenanceToken"), str):
        token = updater._begin_maintenance(restart_tx)
        restart_tx["maintenanceToken"] = token
        restart["phase"] = "held"
        restart_tx["phase"] = "held"
        _persist_transaction(updater, transaction)
    if restart["phase"] in {"held", "restarting"}:
        restart["phase"] = "restarting"
        restart_tx["phase"] = "restarting"
        _persist_transaction(updater, transaction)
        updater._run_systemctl("restart", component["systemdUnit"])
        updater._wait_unit_active(component["systemdUnit"])
        _live_product(updater, product)
        _assert_zero_runtime(updater, plan, allow_maintenance=True)
        restart["phase"] = "end_pending"
        restart_tx["phase"] = "end_pending"
        _persist_transaction(updater, transaction)
    if restart["phase"] == "end_pending":
        updater._end_maintenance(restart_tx, outcome="SUCCESS", healthy=True)
        restart["phase"] = "end_confirmed"
        restart_tx["phase"] = "end_confirmed"
        restart_tx.pop("maintenanceToken", None)
        _persist_transaction(updater, transaction)
    if restart["phase"] != "end_confirmed":
        raise RuntimeError("Durable Yield restart transaction has an unknown recovery phase")
    _assert_yield_post_end_ready(updater, plan, product)


def _assert_yield_post_end_ready(
    updater: Any, plan: dict[str, Any], product: dict[str, Any]
) -> None:
    """Require a fresh READY broker snapshot and live Yield API after restart End."""

    catalog, sources = updater._activity_catalog()
    if catalog.get("generation") != plan.get("catalogGeneration") or sources != plan.get(
        "activitySources"
    ):
        raise RuntimeError("Runtime activity source catalog changed after Yield restart")
    readiness = updater._readiness_for("PACKAGE_ONLY", requires_restart=True, force=True)
    if readiness.get("status") != "READY" or readiness.get("unknown_activity_sources") != []:
        raise RuntimeError("Yield restart has no strict post-End READY source proof")
    for key in (
        "active_task_count",
        "inflight_runtime_admission_count",
        "active_worker_count",
        "active_allocation_count",
    ):
        if type(readiness.get(key)) is not int or readiness[key] != 0:
            raise RuntimeError("Yield restart post-End runtime counts are not known empty")
    _live_product(updater, product)
