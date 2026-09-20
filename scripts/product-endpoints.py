"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: product-endpoints                                          │
│ Role: Start the DirectPlugin endpoints the Products resolve.        │
│ 模块职责：启动产品解析所需的插件端点，并写出 connection_ref 供服务注入。   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CYRENE_ROOT = WORKSPACE_ROOT.parent
PLUGINS_ROOT = Path(
    os.environ.get("CYRENE_PLUGINS_WORKTREE", CYRENE_ROOT / "Cyrene-Plugins-Official")
).expanduser()

ENDPOINTS = (
    (
        "dataset.preparation.v1",
        "dataset_preparation",
        "DatasetPreparationPlugin",
        "CYRENE_DATASET_PREPARATION_CONNECTION_REF",
    ),
    (
        "evaluation.runner.v1",
        "exact_match_evaluator",
        "ExactMatchEvaluationRunner",
        "CYRENE_EVALUATION_RUNNER_CONNECTION_REF",
    ),
    (
        "model.analyzer.v1",
        "hf_model_analyzer",
        "HfModelAnalyzer",
        "CYRENE_MODEL_ANALYZER_CONNECTION_REF",
    ),
    (
        "compatibility.evaluator.v1",
        "compat_rules",
        "CompatibilityRuleEvaluator",
        "CYRENE_COMPATIBILITY_EVALUATOR_CONNECTION_REF",
    ),
    (
        "tool.dataset.validator.v1",
        "dataset_validator",
        "DatasetValidatorPlugin",
        "CYRENE_DATASET_VALIDATOR_CONNECTION_REF",
    ),
    (
        "training.llama-factory.v1",
        "llama_factory",
        "LlamaFactoryTrainingPlugin",
        "CYRENE_LLAMA_FACTORY_CONNECTION_REF",
    ),
)


def dev_home() -> Path:
    configured = os.environ.get("CYRENE_DEV_HOME")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "state" / "cyrene" / "dev"
    )


def _serving_endpoint(capability: str, module: str, attribute: str) -> tuple[object, str]:
    """Start one endpoint in-process and return the server and its reference."""

    from cyrene_plugin_runtime import serve

    owner = getattr(__import__(module, fromlist=[attribute]), attribute)
    server, connection_ref = serve(owner(), capability, "1", "127.0.0.1:0")
    return server, connection_ref


def up() -> int:
    """Serve every declared endpoint in one supervisor process."""

    home = dev_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    references: dict[str, str] = {}
    servers: list[object] = []
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for capability, module, attribute, variable in ENDPOINTS:
            try:
                server, connection_ref = _serving_endpoint(capability, module, attribute)
            except (ImportError, RuntimeError) as exc:
                print(f"{capability}: unavailable ({exc})", file=sys.stderr)
                return 1
            servers.append(server)
            references[variable] = connection_ref
        target = home / "endpoints.json"
        pending = target.with_suffix(".pending")
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(references, stream, indent=2, sort_keys=True)
            stream.write("\n")
        pending.replace(target)
        print(
            json.dumps({"status": "READY", "endpoints": len(references)}, sort_keys=True),
            flush=True,
        )
        while not stopping:
            time.sleep(0.2)
    finally:
        for server in servers:
            server.stop(grace=None).wait()
        (home / "endpoints.json").unlink(missing_ok=True)
    return 0


def status() -> int:
    target = dev_home() / "endpoints.json"
    if not target.is_file():
        print("no plugin endpoints recorded", file=sys.stderr)
        return 1
    print(json.dumps({"status": "READY", "endpoints": json.loads(target.read_text())}, indent=2))
    return 0


def down() -> int:
    """Endpoints run inside the supervisor process, so teardown follows it."""

    target = dev_home() / "endpoints.json"
    if target.is_file():
        target.unlink()
    print("plugin endpoints are released with the supervisor process")
    return 0


def main() -> int:
    arguments = argparse.ArgumentParser(description="Cyrene DirectPlugin endpoint supervisor")
    arguments.add_argument("command", choices=("up", "status", "down"))
    args = arguments.parse_args()
    if args.command == "up":
        return up()
    if args.command == "status":
        return status()
    return down()


if __name__ == "__main__":
    raise SystemExit(main())
