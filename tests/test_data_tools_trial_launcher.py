"""Focused tests for trial generation binding and environment isolation."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cyrene_data_tools_trial_launcher_test",
    WORKSPACE_ROOT / "packaging" / "data_tools_trial.py",
)
assert SPEC is not None and SPEC.loader is not None
trial = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trial
SPEC.loader.exec_module(trial)


def test_generation_config_accepts_a_resolved_local_provider_ref(tmp_path: Path) -> None:
    path = tmp_path / "generation.json"
    configuration = {
        "model_endpoint": "grpc://127.0.0.1:19080",
        "model": "trial-scripted-model",
        "temperature": 0.2,
        "max_tokens_per_call": 64,
        "timeout_seconds": 10,
    }
    path.write_text(json.dumps(configuration), encoding="utf-8")

    loaded = trial._read_generation_config(path)

    assert loaded == configuration
    assert trial._validate_generation_binding_id(trial.DEFAULT_GENERATION_BINDING_ID)
    assert loaded["model_endpoint"] == configuration["model_endpoint"]


@pytest.mark.parametrize(
    "model_endpoint",
    [
        "https://model-provider.example/v1",
        "grpc://provider.example:19080",
        "grpc://user:password@127.0.0.1:19080",
        "grpc://127.0.0.1:19080?token=secret",
    ],
)
def test_generation_config_rejects_remote_or_credential_bearing_refs(
    tmp_path: Path, model_endpoint: str
) -> None:
    path = tmp_path / "generation.json"
    path.write_text(
        json.dumps({"model_endpoint": model_endpoint, "model": "trial-model"}),
        encoding="utf-8",
    )

    with pytest.raises(trial.TrialLauncherError, match="model_endpoint"):
        trial._read_generation_config(path)


def test_generic_plugin_configuration_is_not_inherited_by_trial_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(trial.CAPABILITY_BINDING_ENV, "ambient-binding")
    monkeypatch.setenv(trial.CAPABILITY_CONFIGURATION_ENV, '{"model":"ambient"}')

    environment = trial._runtime_env(None)

    assert trial.CAPABILITY_BINDING_ENV not in environment
    assert trial.CAPABILITY_CONFIGURATION_ENV not in environment


def test_supervised_child_survives_launcher_exit_and_redacts_late_output(
    tmp_path: Path,
) -> None:
    token = "trial-secret-token-that-must-not-reach-the-log"
    log_path = tmp_path / "child.log"
    program_path = tmp_path / "long_lived_child.py"
    program_path.write_text(
        "import json, os, time\n"
        "print(json.dumps({'event':'direct_plugin_ready','capability':'test.capability.v1',"
        "'interface_versions':['1'],'connection_ref':'grpc://127.0.0.1:19081'}), flush=True)\n"
        "time.sleep(0.6)\n"
        "print(os.environ['CYRENE_DATA_TOOLS_TOKEN'], flush=True)\n"
        "print('finished-after-launcher-exit', flush=True)\n",
        encoding="utf-8",
    )
    driver_path = tmp_path / "launch_child.py"
    driver_path.write_text(
        "import importlib.util, pathlib, sys\n"
        f"spec=importlib.util.spec_from_file_location('trial_launcher', {str(WORKSPACE_ROOT / 'packaging' / 'data_tools_trial.py')!r})\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name]=module\n"
        "spec.loader.exec_module(module)\n"
        f"handle=module._launch_process(name='test-child', command=[sys.executable, {str(program_path)!r}], "
        f"cwd=pathlib.Path({str(tmp_path)!r}), environment=__import__('os').environ.copy(), "
        f"log_path=pathlib.Path({str(log_path)!r}), capture_plugin_ready=True)\n"
        "print(handle.process.pid)\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment[trial.TOKEN_ENV] = token

    launch = subprocess.run(
        [sys.executable, str(driver_path)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )
    assert launch.stdout.strip().isdigit()

    ready_path = log_path.with_suffix(log_path.suffix + ".ready.json")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if "finished-after-launcher-exit" in log_path.read_text(encoding="utf-8"):
                break
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    else:
        pytest.fail("supervised child did not finish writing after its launcher exited")

    event = json.loads(ready_path.read_text(encoding="utf-8"))
    assert event["connection_ref"] == "grpc://127.0.0.1:19081"
    log = log_path.read_text(encoding="utf-8")
    assert "[redacted]" in log
    assert token not in log
