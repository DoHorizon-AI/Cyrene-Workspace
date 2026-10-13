"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_exchange_integration_installer.py                           │
│  Module: tests.test_exchange_integration_installer                   │
│  Role: Verify safe host configuration preflight and unit projection. │
│                                                                      │
│  模块职责：验证集成安装前置检查与独立 systemd 单元配置。                 │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


def _preflight_module() -> ModuleType:
    path = WORKSPACE_ROOT / "packaging" / "native_exchange_integration_preflight.py"
    spec = importlib.util.spec_from_file_location(
        "cyrene_exchange_integration_preflight_test", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_passes_host_refs_to_exchange_parser_without_printing_them(
    tmp_path: Path,
) -> None:
    module = _preflight_module()
    configuration = tmp_path / "exchange-integration.env"
    configuration.write_text(
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON='{}'\n",
        encoding="utf-8",
    )
    configuration.chmod(0o644)
    authority_configuration = tmp_path / "workspace-authority.env"
    authority_configuration.write_text(
        'CYRENE_AUTHORITY_PRODUCT_WORKLOAD_PROFILES_JSON=\'[{"workloadId":"exchange-prod-v1"}]\'\n',
        encoding="utf-8",
    )
    authority_configuration.chmod(0o640)
    secrets_file = tmp_path / "exchange-integration-secrets.env"
    secrets_file.write_text("CYRENE_TEST_SECRET='not-for-output'\n", encoding="utf-8")
    secrets_file.chmod(0o600)
    calls: list[dict[str, Any]] = []

    def runner(
        command: list[str],
        *,
        env: dict[str, str],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        calls.append({"command": command, "env": env})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module.run_exchange_integration_preflight(
        configuration_path=configuration,
        secret_path=secrets_file,
        authority_configuration_path=authority_configuration,
        command=Path("/usr/bin/cyrene"),
        runner=runner,
        owner_uid=os.geteuid(),
    )

    assert calls[0]["command"] == ["/usr/bin/cyrene", "service-run", "exchange"]
    assert calls[0]["env"]["CYRENE_EXCHANGE_PROCESS_ROLE"] == "check-config"
    assert calls[0]["env"]["CYRENE_AUTHORITY_PRODUCT_WORKLOAD_PROFILES_JSON"] == (
        '[{"workloadId":"exchange-prod-v1"}]'
    )
    assert calls[0]["env"]["CYRENE_TEST_SECRET"] == "not-for-output"


def test_preflight_failure_never_returns_child_secret_output(tmp_path: Path) -> None:
    module = _preflight_module()
    configuration = tmp_path / "exchange-integration.env"
    configuration.write_text("CYRENE_EXCHANGE_RUNTIME_MODE=combined\n", encoding="utf-8")
    configuration.chmod(0o644)
    authority_configuration = tmp_path / "workspace-authority.env"
    authority_configuration.write_text(
        "CYRENE_AUTHORITY_PRODUCT_WORKLOAD_PROFILES_JSON='[]'\n", encoding="utf-8"
    )
    authority_configuration.chmod(0o640)
    secrets_file = tmp_path / "exchange-integration-secrets.env"
    secrets_file.write_text("CYRENE_TEST_SECRET=secret-marker\n", encoding="utf-8")
    secrets_file.chmod(0o600)

    def runner(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=2, stdout="secret-marker", stderr="secret-marker")

    with pytest.raises(module.ExchangeIntegrationPreflightError) as error:
        module.run_exchange_integration_preflight(
            configuration_path=configuration,
            secret_path=secrets_file,
            authority_configuration_path=authority_configuration,
            command=Path("/usr/bin/cyrene"),
            runner=runner,
            owner_uid=os.geteuid(),
        )

    assert "secret-marker" not in str(error.value)
    assert "No Package Runtime plan was written" in str(error.value)


def test_systemd_units_keep_dispatcher_failure_separate_from_gateway() -> None:
    build_script = (WORKSPACE_ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    catalog = (WORKSPACE_ROOT / "governance" / "component-catalog-v2.json").read_text(
        encoding="utf-8"
    )
    api_start = build_script.index("# cyrene-exchange.service")
    dispatcher_start = build_script.index("# cyrene-exchange-integration-dispatcher.service")
    api_unit = build_script[api_start:dispatcher_start]
    dispatcher_end = build_script.index("# cyrene-catalyst.service", dispatcher_start)
    dispatcher_unit = build_script[dispatcher_start:dispatcher_end]

    assert "Wants=cyrene-exchange-integration-dispatcher.service" in api_unit
    assert "Requires=cyrene-exchange-integration-dispatcher.service" not in api_unit
    assert "Environment=CYRENE_EXCHANGE_PROCESS_ROLE=api" in api_unit
    assert "After=cyrene-exchange.service" in dispatcher_unit
    assert "Requires=cyrene-exchange.service" not in dispatcher_unit
    assert "PartOf=cyrene-exchange.service" not in dispatcher_unit
    assert "ExecCondition=" in dispatcher_unit
    assert "Environment=CYRENE_EXCHANGE_PROCESS_ROLE=dispatcher" in dispatcher_unit
    assert '"auxiliarySystemdUnits": [' in catalog
    assert '"cyrene-exchange-integration-dispatcher.service"' in catalog
