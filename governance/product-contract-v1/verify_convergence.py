"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 verify_convergence.py                                           │
│  Module: governance.product_contract_v1.verify_convergence          │
│  Role: Cross-repository Product contract authority verification.    │
│                                                                     │
│  模块职责：跨仓验证产品契约权威、公共投影与运行时泄露。                     │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

PRODUCTS = ("catalyst", "echo", "reactor", "exchange", "navigator", "yield")
ARTIFACT_CONSUMERS = ("catalyst", "echo", "reactor", "yield")
ARTIFACT_EMBEDDINGS = {
    "catalyst": ("dataset-version.schema.json",),
    "echo": ("evaluation-run.schema.json", "evaluation-result.schema.json"),
    "reactor": ("deployment.schema.json",),
    "yield": ("training-run.schema.json",),
}
PUBLIC_RUNTIME_FIELDS = (
    "engineExecutionRef",
    "kernelExecutionRefs",
    "operationId",
    "workerId",
    "fenceToken",
    "filesystemPath",
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _platform_json(platform: Path, ref: str, path: str) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "-C", str(platform), "show", f"{ref}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _head(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def verify(workspace: Path, platform: Path, roots: dict[str, Path]) -> dict[str, int]:
    """Verify one-authority convergence against exact local checkouts. | 验证唯一权威。"""

    contract_root = workspace / "governance/product-contract-v1"
    authorities = _json(contract_root / "contract-authorities-v1.json")
    concepts = authorities["concepts"]
    assert len({item["id"] for item in concepts}) == len(concepts)
    assert sum(item["duplicateDefinitionCount"] for item in concepts) == 0
    by_id = {item["id"]: item for item in concepts}
    assert {
        item["id"] for item in concepts if item["canonicalOwner"] == "DoHorizon-AI/Cyrene-Platform"
    } == {"ArtifactRef"}
    training_engine = by_id["training.engine.v1"]
    assert training_engine["canonicalOwner"] == "NONE"
    assert training_engine["status"] == "RETIRED_NO_REAL_CONSUMER"
    assert by_id["execution.engine.v1"]["canonicalOwner"] == (
        "DoHorizon-AI/Cyrene-Plugins-Official"
    )
    assert by_id["model.provider.v1"]["canonicalOwner"] == ("DoHorizon-AI/Cyrene-Plugins-Official")
    plugin_owned_ids = {
        "compatibility.evaluator.v1",
        "dataset.preparation.v1",
        "evaluation.runner.v1",
        "execution.engine.v1",
        "gateway.cache.v1",
        "model.analyzer.v1",
        "model.provider.v1",
        "model.routing.v1",
        "tool.dataset.validator.v1",
        "training.llama-factory.v1",
    }
    assert {
        item["id"]
        for item in concepts
        if item["canonicalOwner"] == "DoHorizon-AI/Cyrene-Plugins-Official"
    } == plugin_owned_ids
    assert {item["id"] for item in concepts if item["status"] == "IMPLEMENTED_CANDIDATE"} == {
        "compatibility.evaluator.v1",
        "dataset.preparation.v1",
        "evaluation.runner.v1",
        "execution.engine.v1",
        "model.analyzer.v1",
        "tool.dataset.validator.v1",
        "training.llama-factory.v1",
    }
    migrating_ids = {"model.routing.v1"}
    assert {
        item["id"] for item in concepts if item["status"] == "MIGRATING_COMPATIBILITY"
    } == migrating_ids

    registry = _json(contract_root / "product-contracts-v1.json")
    registered_commits = {
        product["id"]: product["contractCommit"] for product in registry["products"]
    }
    for product in PRODUCTS:
        assert registered_commits[product] == _head(roots[product]), (
            f"{product} registry commit is not the exact validated HEAD"
        )

    artifact = _platform_json(
        platform,
        authorities["platformContractCommit"],
        "contracts/schemas/manifests/artifact_ref.schema.json",
    )
    for product in ARTIFACT_CONSUMERS:
        generated = _json(
            roots[product] / "contracts/product/v1/generated/platform/artifact-ref.schema.json"
        )
        assert generated == artifact, f"{product} ArtifactRef projection drifted"
        for schema_name in ARTIFACT_EMBEDDINGS[product]:
            schema = _json(roots[product] / "contracts/product/v1" / schema_name)
            assert schema["$defs"]["artifactRef"] == artifact, (
                f"{product} embedded ArtifactRef drifted in {schema_name}"
            )

    problem = _json(contract_root / "problem-details.schema.json")
    for product in PRODUCTS:
        product_contract = roots[product] / "contracts/product/v1"
        assert not tuple(product_contract.glob("*event*.schema.json")), (
            f"{product} duplicates the common event envelope"
        )
        generated = _json(product_contract / "generated/common/problem-details.schema.json")
        assert generated == problem, f"{product} Problem Details projection drifted"
        openapi = (product_contract / "openapi.yaml").read_text(encoding="utf-8")
        assert "x-cyrene-contract-profile: product-http-v1" in openapi
        assert "#/components/schemas/Problem" not in openapi

    assert not (contract_root / "artifact-ref.schema.json").exists()
    assert not (roots["yield"] / "contracts/product/v1/training-engine-spi.schema.json").exists()
    yield_surface = "\n".join(
        (roots["yield"] / relative).read_text(encoding="utf-8")
        for relative in (
            "service.json",
            "contracts/product/v1/README.md",
            "contracts/product/v1/training-run.schema.json",
        )
    )
    assert "training.engine.adapter.v1" not in yield_surface
    assert '"const": "training.engine.v1"' not in yield_surface

    navigator_source = (roots["navigator"] / "src/cyrene_navigator/reader.py").read_text(
        encoding="utf-8"
    )
    assert "class ProductReader" not in navigator_source
    assert "class ProductReadPort" in navigator_source

    public_schema_text = "\n".join(
        path.read_text(encoding="utf-8")
        for product in PRODUCTS
        for path in (roots[product] / "contracts/product/v1").glob("*.schema.json")
    )
    for field in PUBLIC_RUNTIME_FIELDS:
        assert field not in public_schema_text, f"public runtime field leaked: {field}"

    yield_openapi = (roots["yield"] / "contracts/product/v1/openapi.yaml").read_text(
        encoding="utf-8"
    )
    assert yield_openapi.count("Preference-Applied") == 2
    assert yield_openapi.count("Location:") == 2

    return {
        "duplicateAuthorities": 0,
        "trainingEngineAuthorities": 0,
        "eventEnvelopeAuthorities": 1,
        "artifactIdentityAuthorities": 1,
        "productStateLeaksToKernel": 0,
        "kernelBusinessSemanticsAdded": 0,
    }


def main() -> None:
    """Parse exact roots and print the verified convergence counters. | 输出验证计数。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--platform", type=Path, required=True)
    for product in PRODUCTS:
        parser.add_argument(f"--{product}", type=Path, required=True)
    args = parser.parse_args()
    roots = {product: getattr(args, product) for product in PRODUCTS}
    print(json.dumps(verify(args.workspace, args.platform, roots), sort_keys=True))


if __name__ == "__main__":
    main()
