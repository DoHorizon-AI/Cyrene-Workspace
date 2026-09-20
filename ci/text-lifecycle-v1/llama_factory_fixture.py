"""Offline ``training.llama-factory.v1`` fixture for the lifecycle harness.

The concrete trainer is Plugins-owned and is not part of the targeted HTTP
lifecycle profile, so this module serves the documented launch contract over a
real DirectPluginRuntime endpoint while the Product-side wiring stays real.
| 该夹具仅在 CI 中提供训练引擎的启动契约，真实训练仍由 Plugins 侧验收覆盖。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from cyrene_plugin_runtime import DirectPayload

CAPABILITY_ID = "training.llama-factory.v1"
INTERFACE_VERSION = "1"
TYPE_PREFIX = f"type.cyrene.io/{CAPABILITY_ID}"


class LlamaFactoryFixturePlugin:
    """Deterministic launch-contract fixture; no model training occurs."""

    plugin_id = "cyrene.fixture.llama-factory"
    version = "0.1.0"
    capabilities = (CAPABILITY_ID,)

    def on_invoke(
        self,
        capability: str,
        action: str,
        payload: bytes,
        *,
        request_type_url: str | None = None,
        stream_results: bool = False,
    ) -> tuple[bool, DirectPayload | str]:
        """Dispatch one typed training-engine request."""

        if capability != CAPABILITY_ID:
            return False, f"INVALID_REQUEST: unsupported capability {capability!r}"
        if action not in {"inspect", "compile", "parse_event"}:
            return False, f"METHOD_NOT_FOUND: unsupported method {action!r}"
        if request_type_url != f"{TYPE_PREFIX}.{action}.request":
            return False, (
                f"INVALID_REQUEST: request_type_url must be {TYPE_PREFIX}.{action}.request"
            )
        if stream_results:
            return False, "METHOD_NOT_SUPPORTED: training methods are not streaming"
        try:
            request = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, f"INVALID_REQUEST: request is not JSON: {exc}"
        if not isinstance(request, dict):
            return False, "INVALID_REQUEST: request must be an object"
        if action == "inspect":
            result = self._inspect()
        elif action == "compile":
            result = self._compile(request)
        else:
            result = self._parse_event(request)
        if result is None:
            return False, "INVALID_REQUEST: request payload is incomplete"
        return True, DirectPayload(
            type_url=f"{TYPE_PREFIX}.{action}.response",
            value=json.dumps(result).encode("utf-8"),
        )

    def _inspect(self) -> dict[str, Any]:
        return {
            "engine": "llamafactory",
            "available": True,
            "version": "0.9.6.dev0-fixture",
            "supported_strategies": ["single", "ddp", "fsdp", "deepspeed", "torchrun"],
            "supported_finetune_types": ["lora", "full", "freeze", "oft"],
            "notes": ["offline contract fixture"],
        }

    def _compile(self, request: dict[str, Any]) -> dict[str, Any] | None:
        spec = request.get("spec")
        if not isinstance(spec, dict):
            return None
        output = Path(spec["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        extras = dict(spec.get("extra") or {})
        arguments = dict(extras.get("llamafactory_args") or {})
        if extras.get("max_steps") is not None:
            arguments["max_steps"] = extras["max_steps"]
        if extras.get("max_train_samples") is not None:
            arguments["max_samples"] = extras["max_train_samples"]
        artifact = output / "llamafactory_train.json"
        artifact.write_text(json.dumps(arguments), encoding="utf-8")
        distributed = spec["distributed"]
        return {
            "launch": {
                "engine": "llamafactory",
                "argv": [sys.executable, "-m", "llamafactory.cli", "train", str(artifact)],
                "work_dir": str(output),
                "cwd": str(output),
                "distributed": distributed,
                "checkpoint": spec["checkpoint"],
                "launch_kind": "direct",
                "env": {},
                "resources": {
                    "gpu_count": distributed["gpu_count"],
                    "world_size": distributed["world_size"],
                    "nnodes": distributed["nnodes"],
                    "nproc_per_node": distributed["nproc_per_node"],
                    "gpu_memory_gb": 0.0,
                },
                "mounts": [
                    {
                        "source": spec["dataset"]["path"],
                        "target": "/input/dataset",
                        "kind": "bind",
                        "read_only": True,
                    },
                    {
                        "source": str(output),
                        "target": "/output",
                        "kind": "bind",
                        "read_only": False,
                    },
                ],
                "output_layout": {"root": ""},
                "spec_artifact_path": str(artifact),
                "extra": {},
            }
        }

    def _parse_event(self, request: dict[str, Any]) -> dict[str, Any] | None:
        line = request.get("line")
        if not isinstance(line, str):
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "loss" in payload:
            return {"kind": "progress", "message": line, "payload": payload, "raw": line}
        return {"kind": "log", "message": line, "raw": line}
