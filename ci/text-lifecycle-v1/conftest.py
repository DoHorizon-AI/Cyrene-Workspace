"""Require exact Product checkouts for integration tests. | 强制使用指定产品检出。"""

import os
import sys
from pathlib import Path
from typing import Any

from cyrene_plugin_runtime import serve

DATASET_PREPARATION_CONNECTION_ENV = "CYRENE_DATASET_PREPARATION_CONNECTION_REF"
EVALUATION_RUNNER_CONNECTION_ENV = "CYRENE_EVALUATION_RUNNER_CONNECTION_REF"
MODEL_ANALYZER_CONNECTION_ENV = "CYRENE_MODEL_ANALYZER_CONNECTION_REF"
COMPATIBILITY_EVALUATOR_CONNECTION_ENV = "CYRENE_COMPATIBILITY_EVALUATOR_CONNECTION_REF"
DATASET_VALIDATOR_CONNECTION_ENV = "CYRENE_DATASET_VALIDATOR_CONNECTION_REF"
LLAMA_FACTORY_CONNECTION_ENV = "CYRENE_LLAMA_FACTORY_CONNECTION_REF"

_PLUGIN_SERVERS: list[Any] = []

_OWNER_ENDPOINTS = (
    (
        "dataset.preparation.v1",
        "dataset_preparation",
        "DatasetPreparationPlugin",
        DATASET_PREPARATION_CONNECTION_ENV,
    ),
    (
        "evaluation.runner.v1",
        "exact_match_evaluator",
        "ExactMatchEvaluationRunner",
        EVALUATION_RUNNER_CONNECTION_ENV,
    ),
    (
        "model.analyzer.v1",
        "hf_model_analyzer",
        "HfModelAnalyzer",
        MODEL_ANALYZER_CONNECTION_ENV,
    ),
    (
        "compatibility.evaluator.v1",
        "compat_rules",
        "CompatibilityRuleEvaluator",
        COMPATIBILITY_EVALUATOR_CONNECTION_ENV,
    ),
    (
        "tool.dataset.validator.v1",
        "dataset_validator",
        "DatasetValidatorPlugin",
        DATASET_VALIDATOR_CONNECTION_ENV,
    ),
    (
        "training.llama-factory.v1",
        "llama_factory",
        "LlamaFactoryTrainingPlugin",
        LLAMA_FACTORY_CONNECTION_ENV,
    ),
)

for repository, source in (
    ("CATALYST", "src"),
    ("YIELD", "training/core/src"),
    ("YIELD", "sdk/python/cyrene_yield_contracts/src"),
    ("REACTOR", "product/src"),
    ("NAVIGATOR", "src"),
    ("ECHO", "src"),
):
    selected = Path(os.environ[f"CYRENE_{repository}_WORKTREE"]).resolve() / source
    if not selected.is_dir():
        raise RuntimeError("Missing exact Product checkout: " + repository)
    sys.path.insert(0, str(selected))


def _serve(capability: str, module: str, attribute: str, environment: str) -> None:
    """Start one canonical Plugin owner endpoint and publish its connection reference.

    中文：启动一个规范的 Plugin owner endpoint，并发布其 connection reference。
    """

    owner = getattr(__import__(module, fromlist=[attribute]), attribute)
    server, connection_ref = serve(owner(), capability, "1", "127.0.0.1:0")
    _PLUGIN_SERVERS.append(server)
    os.environ[environment] = connection_ref


def pytest_configure() -> None:
    """Start the canonical Plugin owner endpoints the Products resolve against.

    Every Product resolves its capability engines from opaque connection
    references, so the cross-repository fixtures must serve the real owner
    packages instead of any in-repository capability implementation.  The
    LLaMA-Factory trainer is the real Plugins package; only its accelerator
    process is out of scope for this profile.

    中文：每个 Product 都通过不透明 connection references 解析自己的 capability engines。因此，跨仓 fixture 必须服务真实 owner packages，不能改用仓库内部的 capability 实现。LLaMA-Factory trainer 使用真实 Plugins package；本 profile 不负责其 accelerator process。
    """

    for capability, module, attribute, environment in _OWNER_ENDPOINTS:
        _serve(capability, module, attribute, environment)


def pytest_unconfigure() -> None:
    """Stop every Plugin owner endpoint after the test session.

    中文：测试会话结束后，停止所有 Plugin owner endpoints。
    """

    for _capability, _module, _attribute, environment in _OWNER_ENDPOINTS:
        os.environ.pop(environment, None)
    os.environ.pop(LLAMA_FACTORY_CONNECTION_ENV, None)
    for server in _PLUGIN_SERVERS:
        server.stop(grace=None).wait()
    _PLUGIN_SERVERS.clear()
