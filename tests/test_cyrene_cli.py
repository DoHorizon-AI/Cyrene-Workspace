"""Unified Cyrene CLI data-safety tests. | Cyrene 统一 CLI 数据安全测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
from argparse import Namespace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import ClassVar

import pytest

WORKSPACE_ROOT = Path(__file__).parents[1]


def _module() -> ModuleType:
    path = WORKSPACE_ROOT / "cyrene"
    loader = SourceFileLoader("cyrene_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _set_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state" / "cyrene" / "dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("CYRENE_DEV_HOME", str(home))
    return home


def test_workload_cli_accepts_guided_install_and_exact_component_uninstall() -> None:
    module = _module()

    install = module.build_parser().parse_args(["workload", "install", "catalyst", "--yes"])
    uninstall = module.build_parser().parse_args(
        ["workload", "uninstall", "cyrene-tools-dataset-preparation", "--workload", "plugins"]
    )

    assert install.workload_action == "install"
    assert install.workload_id == "catalyst"
    assert install.yes is True
    assert uninstall.workload_action == "uninstall"
    assert uninstall.workload_id == "cyrene-tools-dataset-preparation"
    assert uninstall.workload_owner == "plugins"


def test_backup_rejects_destination_inside_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    (home / "artifact.txt").write_text("important", encoding="utf-8")
    target = home / "backups" / "unsafe.tar.gz"

    assert module.cmd_backup(Namespace(dest=str(target))) == 1
    assert not target.exists()
    assert not target.parent.exists()


def test_backup_is_created_with_canonical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    (home / "artifact.txt").write_text("important", encoding="utf-8")
    target = tmp_path / "safe.tar.gz"

    assert module.cmd_backup(Namespace(dest=str(target))) == 0

    with tarfile.open(target, "r:gz") as archive:
        names = archive.getnames()
    assert names[0] == "cyrene-dev"
    assert "cyrene-dev/artifact.txt" in names
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_restore_rejects_path_traversal_and_preserves_existing_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    existing = home / "existing.txt"
    existing.write_text("preserve", encoding="utf-8")
    archive_path = tmp_path / "malicious.tar.gz"
    payload = b"escaped"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("cyrene-dev/../../escaped.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    assert module.cmd_restore(Namespace(src=str(archive_path))) == 1
    assert existing.read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / "escaped.txt").exists()


def test_restore_atomically_replaces_stale_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    (home / "stale.txt").write_text("old", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "restored.txt").write_text("new", encoding="utf-8")
    archive_path = tmp_path / "valid.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname="cyrene-dev")

    assert module.cmd_restore(Namespace(src=str(archive_path))) == 0
    assert (home / "restored.txt").read_text(encoding="utf-8") == "new"
    assert not (home / "stale.txt").exists()
    assert home.stat().st_mode & 0o077 == 0


def test_restore_rejects_related_directory_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    existing = home / "existing.txt"
    existing.write_text("preserve", encoding="utf-8")

    assert module.cmd_restore(Namespace(src=str(home.parent))) == 1
    assert existing.read_text(encoding="utf-8") == "preserve"


def test_logs_run_prints_diagnostics_and_flags_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    _set_home(tmp_path, monkeypatch)
    seen: list[str] = []

    def fake_request(url: str, timeout: float = 15.0, headers: dict | None = None) -> dict:
        seen.append(url)
        return {
            "resourceId": "abc",
            "items": [
                {
                    "sequence": 1,
                    "timestamp": "2026-09-22T10:00:00+00:00",
                    "level": "error",
                    "source": "trainer",
                    "stream": "stderr",
                    "code": "CUDA_OOM",
                    "message": "CUDA out of memory",
                    "truncated": False,
                }
            ],
            "nextSequence": 1,
            "terminal": True,
            "diagnosticsDegraded": True,
        }

    monkeypatch.setattr(module, "_request_json", fake_request)
    code = module.cmd_logs(
        Namespace(
            service=None, run="abc", deployment=None, trace=None, json=False, lines=50, follow=False
        )
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "CUDA out of memory" in captured.out
    assert "ERROR" in captured.out
    assert "degraded" in captured.err
    assert seen[0].endswith("/api/v1/training-runs/abc/diagnostics?afterSequence=0&limit=500")


def test_logs_trace_reports_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    logs = home / "logs"
    logs.mkdir(parents=True)
    (logs / "yield.stderr.log").write_text(
        "trace=4bf92f3577b34da6a3ce929d0e0e4736 boom\nother line\n", encoding="utf-8"
    )

    found = module.cmd_logs(
        Namespace(
            service=None,
            run=None,
            deployment=None,
            trace="4bf92f3577b34da6a3ce929d0e0e4736",
            json=True,
            lines=50,
            follow=False,
        )
    )
    assert found == 0
    assert "boom" in capsys.readouterr().out

    missing = module.cmd_logs(
        Namespace(
            service=None,
            run=None,
            deployment=None,
            trace="ffffffffffffffffffffffffffffffff",
            json=True,
            lines=50,
            follow=False,
        )
    )
    assert missing == 1
    assert "No local record of trace" in capsys.readouterr().out


def test_diagnostics_collect_writes_a_private_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    _set_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module,
        "_request_json",
        lambda url, timeout=15.0, headers=None: {
            "items": [
                {
                    "sequence": 1,
                    "timestamp": "t",
                    "level": "warn",
                    "stream": "stderr",
                    "message": "boom",
                }
            ],
            "nextSequence": 1,
            "terminal": True,
            "diagnosticsDegraded": False,
        },
    )
    output = tmp_path / "bundle.json"
    code = module.cmd_diagnostics_collect(
        Namespace(run="abc", deployment=None, trace=None, output=str(output))
    )
    assert code == 0
    assert output.stat().st_mode & 0o777 == 0o600
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert bundle["correlation"] == {"run": "abc"}
    assert bundle["cleanup"]["confirmed"] is True
    assert bundle["diagnostics"][0]["message"] == "boom"
    assert "versions" in bundle
    assert "not uploaded" in capsys.readouterr().out


def test_diagnostics_output_is_repaired_to_owner_only(tmp_path: Path) -> None:
    module = _module()
    existing = tmp_path / "loose.json"
    existing.write_text("{}", encoding="utf-8")
    existing.chmod(0o644)
    module._write_private_json(existing, {"a": 1})
    assert existing.stat().st_mode & 0o777 == 0o600


def test_bootstrap_check_reports_missing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    root = tmp_path / "cyrene-install"
    root.mkdir()
    monkeypatch.setenv("CYRENE_INSTALL_ROOT", str(root))
    assert module.cmd_bootstrap(Namespace(check=True, repair=False)) == 1
    out = capsys.readouterr().out
    assert "No bootstrap marker" in out
    assert "ISSUES FOUND" in out


def test_component_run_uses_catalog_id_fixed_install_root_and_unit_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    calls: dict[str, object] = {}

    class FakeUpdater:
        components: ClassVar[dict[str, dict[str, object]]] = {
            "cyrene-kernel": {
                "componentId": "cyrene-kernel",
                "kind": "native-binary",
                "systemdUnit": "cyrene-kernel.service",
            }
        }

        @staticmethod
        def _target_for(component: dict[str, str]) -> dict[str, str]:
            return {"artifactKind": "native-binary"}

    class FakeUpdates:
        DEFAULT_INSTALL_ROOT = Path("/usr/lib/cyrene")

        @staticmethod
        def ComponentUpdater() -> FakeUpdater:
            return FakeUpdater()

        @staticmethod
        def run_component(
            component_id: str, *, install_root: Path, startup_arguments: list[str]
        ) -> int:
            calls.update(
                component_id=component_id,
                install_root=install_root,
                startup_arguments=startup_arguments,
            )
            return 0

    monkeypatch.setattr(module, "_load_component_updates_module", lambda: FakeUpdates)
    monkeypatch.setenv("CYRENE_INSTALL_ROOT", str(tmp_path / "caller-selected-root"))
    args = module.build_parser().parse_args(
        [
            "component-run",
            "cyrene-kernel",
            "--",
            "--listen-socket",
            "/run/cyrene/kernel.sock",
        ]
    )

    assert args.func(args) == 0
    assert calls == {
        "component_id": "cyrene-kernel",
        "install_root": Path("/usr/lib/cyrene"),
        "startup_arguments": ["--listen-socket", "/run/cyrene/kernel.sock"],
    }


def test_component_run_rejects_ids_not_in_the_trusted_native_catalog(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()

    class FakeUpdater:
        components: ClassVar[dict[str, dict[str, str]]] = {}

    class FakeUpdates:
        DEFAULT_INSTALL_ROOT = Path("/usr/lib/cyrene")
        ComponentUpdater = FakeUpdater

        @staticmethod
        def run_component(*args: object, **kwargs: object) -> int:
            pytest.fail("unknown component IDs must be rejected before execution")

    monkeypatch.setattr(module, "_load_component_updates_module", lambda: FakeUpdates)
    result = module.cmd_component_run(
        Namespace(component_id="cyrene-untrusted", startup_arguments=[])
    )

    assert result == 1
    assert "not a supported native systemd component" in capsys.readouterr().err


def test_service_prepare_uses_signed_runtime_helper_and_prints_descriptor_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    calls: dict[str, object] = {}
    install_root = tmp_path / "install"
    data_root = tmp_path / "state"
    monkeypatch.setenv("CYRENE_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("CYRENE_DATA_DIR", str(data_root))

    class FakeBundle:
        @staticmethod
        def prepare_execution_runtime(service: str, **kwargs: object) -> dict[str, str]:
            calls.update(service=service, **kwargs)
            return {
                "status": "prepared",
                "service": service,
                "runtimeHome": str(data_root / "trainer-runtime" / "1.2.3"),
                "runtimeManifest": str(data_root / "trainer-runtime" / "1.2.3" / "runtime.json"),
            }

    monkeypatch.setattr(module, "_load_service_bundle_module", lambda: FakeBundle)
    args = module.build_parser().parse_args(["service-prepare", "yield", "--version", "1.2.3"])

    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["runtimeHome"].endswith("/trainer-runtime/1.2.3")
    assert calls == {
        "service": "yield",
        "version": "1.2.3",
        "install_root": install_root,
        "data_root": data_root,
    }


def test_service_prepare_reports_invalid_runtime_descriptor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()

    class FakeBundle:
        @staticmethod
        def prepare_execution_runtime(service: str, **kwargs: object) -> dict[str, str]:
            raise ValueError("release has no execution_runtime descriptor")

    monkeypatch.setattr(module, "_load_service_bundle_module", lambda: FakeBundle)
    args = module.build_parser().parse_args(["service-prepare", "reactor"])

    assert args.func(args) == 1
    assert "no execution_runtime descriptor" in capsys.readouterr().err


def test_runtime_maintenance_bootstrap_requires_root_before_loading_or_reading(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        module,
        "_load_native_component_bootstrap_module",
        lambda: pytest.fail("unprivileged calls must fail before loading the helper"),
    )
    args = module.build_parser().parse_args(
        [
            "component-bootstrap-runtime-maintenance",
            "--index",
            "/missing/index.json",
            "--index-attestation",
            "/missing/index.sigstore",
            "--manifest",
            "/missing/manifest.json",
            "--artifact",
            "/missing/artifact.tar",
            "--artifact-attestation",
            "/missing/artifact.sigstore",
            "--channel",
            "stable",
            "--target-id",
            "linux-x86_64",
        ]
    )

    assert args.func(args) == 1
    assert "must run as root" in capsys.readouterr().err


def test_runtime_maintenance_bootstrap_reports_missing_fixed_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        module,
        "_load_native_component_bootstrap_module",
        lambda: (_ for _ in ()).throw(FileNotFoundError("fixed helper missing")),
    )
    args = module.build_parser().parse_args(
        [
            "component-bootstrap-runtime-maintenance",
            "--index",
            "/missing/index.json",
            "--index-attestation",
            "/missing/index.sigstore",
            "--manifest",
            "/missing/manifest.json",
            "--artifact",
            "/missing/artifact.tar",
            "--artifact-attestation",
            "/missing/artifact.sigstore",
            "--channel",
            "stable",
            "--target-id",
            "linux-x86_64",
        ]
    )

    assert args.func(args) == 1
    assert "fixed helper missing" in capsys.readouterr().err


def test_runtime_maintenance_bootstrap_forwards_exact_bytes_and_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    payloads = {
        "index": b'{"schemaVersion":1}',
        "index-attestation": b"index-attestation",
        "manifest": b'{"manifestDigest":"sha256:abc"}',
        "artifact": b"artifact-bytes",
        "artifact-attestation": b"artifact-attestation",
    }
    paths: dict[str, Path] = {}
    for name, payload in payloads.items():
        path = tmp_path / name
        path.write_bytes(payload)
        paths[name] = path
    calls: list[dict[str, object]] = []
    updater = object()

    class FakeUpdates:
        @staticmethod
        def ComponentUpdater() -> object:
            return updater

    class FakeBootstrap:
        @staticmethod
        def bootstrap_verified_runtime_maintenance(received_updater: object, **kwargs: object):
            assert received_updater is updater
            calls.append(kwargs)
            if kwargs["confirm_plan_digest"] is None:
                return {"status": "confirmation-required", "planDigest": "sha256:plan"}
            return {
                "status": "activated",
                "planDigest": kwargs["confirm_plan_digest"],
                "activePointer": "/usr/lib/cyrene/active",
            }

    monkeypatch.setattr(module, "_load_native_component_bootstrap_module", lambda: FakeBootstrap)
    monkeypatch.setattr(module, "_load_component_updates_module", lambda: FakeUpdates)
    common = [
        "component-bootstrap-runtime-maintenance",
        "--index",
        str(paths["index"]),
        "--index-attestation",
        str(paths["index-attestation"]),
        "--manifest",
        str(paths["manifest"]),
        "--artifact",
        str(paths["artifact"]),
        "--artifact-attestation",
        str(paths["artifact-attestation"]),
        "--channel",
        "preview",
        "--target-id",
        "linux-x86_64",
    ]
    args = module.build_parser().parse_args(common)
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "confirmation-required",
        "planDigest": "sha256:plan",
    }

    confirmed_args = module.build_parser().parse_args(
        [*common, "--confirm-plan-digest", "sha256:plan"]
    )
    assert confirmed_args.func(confirmed_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "activated"
    assert calls == [
        {
            "index_bytes": payloads["index"],
            "index_attestation_bytes": payloads["index-attestation"],
            "manifest_bytes": payloads["manifest"],
            "artifact_bytes": payloads["artifact"],
            "artifact_attestation_bytes": payloads["artifact-attestation"],
            "channel": "preview",
            "target_id": "linux-x86_64",
            "confirm_plan_digest": None,
        },
        {
            "index_bytes": payloads["index"],
            "index_attestation_bytes": payloads["index-attestation"],
            "manifest_bytes": payloads["manifest"],
            "artifact_bytes": payloads["artifact"],
            "artifact_attestation_bytes": payloads["artifact-attestation"],
            "channel": "preview",
            "target_id": "linux-x86_64",
            "confirm_plan_digest": "sha256:plan",
        },
    ]


def test_install_missing_component_units_verifies_payload_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    updates_path = WORKSPACE_ROOT / "packaging" / "component_updates.py"
    updates_loader = SourceFileLoader("cyrene_component_updates_units_test", str(updates_path))
    updates_spec = importlib.util.spec_from_loader(updates_loader.name, updates_loader)
    assert updates_spec is not None
    updates = importlib.util.module_from_spec(updates_spec)
    sys.modules[updates_spec.name] = updates
    updates_loader.exec_module(updates)

    component_id = "cyrene-kernel"
    unit = "cyrene-kernel.service"
    version = "1.2.3"
    install_root_path = tmp_path / "install"
    release_root = install_root_path / "components" / component_id / "releases"
    release = release_root / version
    unit_bytes = b"[Service]\nExecStart=/usr/bin/cyrene component-run cyrene-kernel\n"
    entrypoint_bytes = b"kernel-binary"
    files = {
        "bin/cyrene-kernel": "sha256:" + hashlib.sha256(entrypoint_bytes).hexdigest(),
        f"systemd/{unit}": "sha256:" + hashlib.sha256(unit_bytes).hexdigest(),
    }
    artifact = {
        "kind": "native-binary",
        "sha256": "sha256:" + "a" * 64,
        "entrypoint": "bin/cyrene-kernel",
        "files": files,
    }
    manifest = {
        "schemaVersion": 1,
        "componentId": component_id,
        "version": version,
        "artifact": artifact,
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    (release / "bin").mkdir(parents=True)
    (release / "systemd").mkdir()
    entrypoint_path = release / "bin" / "cyrene-kernel"
    unit_payload_path = release / "systemd" / unit
    entrypoint_path.write_bytes(entrypoint_bytes)
    entrypoint_path.chmod(0o755)
    unit_payload_path.write_bytes(unit_bytes)
    unit_payload_path.chmod(0o644)
    manifest_path = release / "component-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o644)
    active = install_root_path / "components" / component_id / "active"
    active.symlink_to(f"releases/{version}")
    for directory in (
        install_root_path,
        install_root_path / "components",
        install_root_path / "components" / component_id,
        release_root,
        release,
        release / "bin",
        release / "systemd",
    ):
        directory.chmod(0o755)

    class FakeUpdater:
        components: ClassVar[dict[str, dict[str, object]]] = {
            component_id: {
                "componentId": component_id,
                "publisher": "DoHorizon-AI/Cyrene-Platform",
                "kind": "native-binary",
                "systemdUnit": unit,
                "targets": [
                    {
                        "targetId": "linux-ubuntu-24.04-x86_64-systemd",
                        "artifactKind": "native-binary",
                        "support": "supported",
                    }
                ],
            },
            "cy-runtime-agent": {
                "componentId": "cy-runtime-agent",
                "publisher": "DoHorizon-AI/Cyrene-Platform",
                "kind": "native-binary",
                "systemdUnit": "cy-runtime-agent.service",
                "targets": [
                    {
                        "targetId": "linux-ubuntu-24.04-x86_64-systemd",
                        "artifactKind": "native-binary",
                        "support": "supported",
                    }
                ],
            },
        }

        @staticmethod
        def _validate_manifest_digest(value: dict[str, object], expected: str) -> None:
            updates.ComponentUpdater._validate_manifest_digest(FakeUpdater, value, expected)

        @staticmethod
        def _target_for(component: dict[str, object]) -> dict[str, str]:
            return {"artifactKind": "native-binary"}

    real_lstat = Path.lstat
    privileged_paths = {
        install_root_path,
        install_root_path / "components",
        install_root_path / "components" / component_id,
        release_root,
        release,
        active,
    }
    unit_dir = tmp_path / "usr" / "lib" / "systemd" / "system"
    unit_dir.mkdir(parents=True)
    unit_dir.chmod(0o755)
    privileged_paths.add(unit_dir)

    def root_owned_lstat(path: Path) -> os.stat_result:
        info = real_lstat(path)
        if (
            path in privileged_paths
            or path.is_relative_to(release)
            or path.is_relative_to(unit_dir)
        ):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)  # type: ignore[return-value]
        return info

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    search_directories = (tmp_path / "etc" / "systemd", unit_dir)

    installed, existing = module.install_missing_component_units(
        updates,
        FakeUpdater(),
        install_root_path=install_root_path,
        destination=unit_dir,
        search_directories=search_directories,
    )
    target_unit = unit_dir / unit
    assert installed == [unit]
    assert existing == []
    assert target_unit.read_bytes() == unit_bytes
    assert stat.S_IMODE(target_unit.stat().st_mode) == 0o644

    installed, existing = module.install_missing_component_units(
        updates,
        FakeUpdater(),
        install_root_path=install_root_path,
        destination=unit_dir,
        search_directories=search_directories,
    )
    assert installed == []
    assert existing == [unit]

    target_unit.write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="conflicts with the trusted payload"):
        module.install_missing_component_units(
            updates,
            FakeUpdater(),
            install_root_path=install_root_path,
            destination=unit_dir,
            search_directories=search_directories,
        )


def test_bootstrap_check_verifies_pinned_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    root = tmp_path / "cyrene-install"
    (root / "runtime-venv" / "bin").mkdir(parents=True)
    (root / "release-lock.json").write_text(
        json.dumps(
            {"engines": {"execution.engine.v1": {"package": "vllm", "acceptedVersion": "0.25.1"}}}
        ),
        encoding="utf-8",
    )
    (root / "bootstrap-state.json").write_text(json.dumps({"state": "READY"}), encoding="utf-8")
    fake_python = root / "runtime-venv" / "bin" / "python"
    fake_python.write_text(
        "#!/bin/sh\necho 'vllm 0.25.1'\necho 'llamafactory 0.9.5'\n", encoding="utf-8"
    )
    fake_python.chmod(0o755)
    monkeypatch.setenv("CYRENE_INSTALL_ROOT", str(root))

    assert module.cmd_bootstrap(Namespace(check=True, repair=False)) == 0
    out = capsys.readouterr().out
    assert "vllm: 0.25.1" in out
    assert "llamafactory: 0.9.5" in out
    assert "READY" in out


def test_pair_code_is_printed_only_to_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captured output (a journal, a log file) must not receive the code.

    中文:捕获的输出(例如 journal 或日志文件)不能收到该 code。
    """

    module = _module()
    home = tmp_path / "state" / "cyrene" / "dev"
    home.mkdir(parents=True)

    monkeypatch.setattr(module.sys.stdout, "isatty", lambda: True, raising=False)
    interactive = module._pair_code_notice(home, "ABCDEF")
    assert "ABCDEF" in interactive

    monkeypatch.setattr(module.sys.stdout, "isatty", lambda: False, raising=False)
    captured = module._pair_code_notice(home, "ABCDEF")
    assert "ABCDEF" not in captured
    assert str(home / "pair_code.txt") in captured
