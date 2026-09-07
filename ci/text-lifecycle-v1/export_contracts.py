"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: Text Lifecycle V1 contract export                          │
│ Role: Freeze implemented actions and reference the Platform schema.│
│ 模块职责：从产品模型导出新接口；Artifact/ModelVersion 引用平台契约。    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml


def references(value: Any) -> set[str]:
    if isinstance(value, dict):
        result = (
            {value["$ref"].split("/")[-1]}
            if str(value.get("$ref", "")).startswith("#/components/schemas/")
            else set()
        )
        for child in value.values():
            result.update(references(child))
        return result
    if isinstance(value, list):
        return set().union(*(references(child) for child in value))
    return set()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export implemented Product lifecycle contract additions"
    )
    parser.add_argument(
        "--product",
        choices=["catalyst", "yield", "reactor", "navigator", "echo"],
        required=True,
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--platform", type=Path, required=True)
    args = parser.parse_args()
    source = (
        "product/src"
        if args.product == "reactor"
        else "training/core/src"
        if args.product == "yield"
        else "src"
    )
    sys.path.insert(0, str(args.root / source))
    module = {
        "yield": "cy_exec.training.product_api",
        "reactor": "cyrene_reactor_product.api",
        "navigator": "cyrene_navigator.persistence.api",
    }.get(args.product, f"cyrene_{args.product}.api")
    factory = getattr(
        importlib.import_module(module),
        "create_persistence_app" if args.product == "navigator" else "create_app",
    )
    with TemporaryDirectory(prefix="cyrene-contract-") as temporary:
        scratch = Path(temporary)
        if args.product == "yield":
            app = factory(state_directory=scratch, artifact_root=scratch / "artifacts")
        elif args.product == "navigator":
            app = factory(scratch / "product.db", {})
        else:
            kwargs = {"database_path": scratch / "product.db"}
            if args.product != "reactor":
                kwargs["artifact_root"] = scratch / "artifacts"
            app = factory(**kwargs)
        generated = app.openapi()
    root = args.root / "contracts/product/v1"
    target = root / (
        "persistence.openapi.yaml"
        if args.product == "navigator"
        else "openapi.yaml"
        if args.product == "yield"
        else "lifecycle.openapi.yaml"
    )
    original_path = root / "openapi.yaml"
    original = yaml.safe_load(original_path.read_text())
    if args.product in {"yield", "navigator"}:
        paths = generated["paths"]
    else:
        existing = set(original["paths"])
        # Already-linked paths remain included on a repeat export.
        paths = {
            path: document
            for path, document in generated["paths"].items()
            if path not in existing
            or "$ref" in original["paths"][path]
            or path.endswith("/yield-draft")
        }
    all_schemas = generated["components"]["schemas"]
    names = references(paths)
    while True:
        expanded = names | references({key: all_schemas[key] for key in names})
        if expanded == names:
            break
        names = expanded
    schemas = {name: copy.deepcopy(all_schemas[name]) for name in sorted(names)}
    if "ArtifactRef" in schemas:
        schemas["ArtifactRef"] = {"$ref": "./generated/platform/artifact-ref.schema.json"}
    canonical = {
        "Deployment": "deployment.schema.json",
        "EvaluationRun": "evaluation-run.schema.json",
    }
    for name, filename in canonical.items():
        if name in schemas and (root / filename).exists():
            schemas[name] = {"$ref": "./" + filename}
    for schema in schemas.values():
        properties = schema.get("properties", {})
        if "modelVersion" in properties:
            properties["modelVersion"] = {"$ref": "./generated/platform/model_version.schema.json"}
    problem = {
        "description": "Product action rejected (RFC 9457)",
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "./generated/common/problem-details.schema.json"}
            },
        },
    }
    for item in paths.values():
        for method, operation in item.items():
            if method in {"get", "post", "patch", "put", "delete"}:
                for status in ("404", "409", "422", "502", "503"):
                    operation["responses"][status] = copy.deepcopy(problem)
    document = {
        "openapi": "3.1.2",
        "x-cyrene-contract-profile": "product-http-v1",
        "info": generated["info"],
        "paths": paths,
        "components": {"schemas": schemas},
    }
    header = (
        "# Generated from implemented Product actions; "
        "Artifact and ModelVersion remain Platform-owned.\n"
    )
    target.write_text(
        header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
    )
    if args.product == "yield":
        original_path.write_text(
            header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100)
        )
    elif args.product != "navigator":
        text = original_path.read_text()
        for path in paths:
            pointer = path.replace("~", "~0").replace("/", "~1")
            entry = f"  {path}:\n    $ref: './{target.name}#/paths/{pointer}'\n"
            pattern = re.compile(
                r"^  " + re.escape(path) + r":\n.*?(?=^  /|^components:|\Z)",
                re.M | re.S,
            )
            if pattern.search(text):
                text = pattern.sub(lambda _match: entry, text)
            else:
                text = text.replace("components:\n", entry + "components:\n", 1)
        if args.product == "catalyst":
            # Add the Product provenance projection to existing preparation responses.
            parsed = yaml.safe_load(text)
            for name in ("Preparation", "DatasetVersionSummary"):
                properties = parsed["components"]["schemas"].get(name, {}).get("properties", {})
                if properties and "sourceRefs" not in properties:
                    anchor = f"    {name}:\n"
                    start = text.index(anchor)
                    at = text.index("      properties:\n", start) + len("      properties:\n")
                    text = (
                        text[:at]
                        + "        sourceRefs: {type: array, items: {type: string, format: uri}}\n"
                        + text[at:]
                    )
        original_path.write_text(text)
    if args.product in {"yield", "reactor"}:
        destination = root / "generated/platform"
        files = {}
        for name in ("model_version.schema.json", "artifact_ref.schema.json"):
            payload = (args.platform / "contracts/schemas/manifests" / name).read_bytes()
            (destination / name).write_bytes(payload)
            files[name] = hashlib.sha256(payload).hexdigest()
        (destination / "lifecycle-sources.json").write_text(
            json.dumps(
                {
                    "repository": "DoHorizon-AI/Cyrene-Platform",
                    "revision": "a29fb0bbb130876429ac4ac80a459d45d59c7c32",
                    "sha256": files,
                },
                indent=2,
            )
            + "\n"
        )
    if args.product == "yield":
        models = importlib.import_module("cy_exec.training.product_models")
        result_schema = models.TrainingResultResource.model_json_schema(by_alias=True)
        result_schema.update(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://schemas.cyrene.dev/yield/product/v1/training-result.schema.json",
            }
        )
        result_schema["properties"]["modelVersion"] = {
            "$ref": "./generated/platform/model_version.schema.json"
        }
        result_schema["$defs"]["ArtifactRef"] = {
            "$ref": "./generated/platform/artifact-ref.schema.json"
        }
        (root / "training-result.schema.json").write_text(
            json.dumps(result_schema, indent=2) + "\n"
        )
        run_path = root / "training-run.schema.json"
        run_schema = json.loads(run_path.read_text())
        for name in ("resourceRef", "draftRef"):
            run_schema["properties"][name] = {"$ref": "#/$defs/resourceRef"}
        run_schema["properties"]["result"] = {"$ref": "./training-result.schema.json"}
        run_schema["$defs"]["resourceRef"] = models.ResourceRef.model_json_schema(by_alias=True)
        run_path.write_text(json.dumps(run_schema, indent=2) + "\n")


if __name__ == "__main__":
    main()
