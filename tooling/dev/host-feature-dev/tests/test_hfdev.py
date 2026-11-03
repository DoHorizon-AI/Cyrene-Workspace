"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_hfdev.py                                                        │
│  Module: tooling.dev.host_feature_dev.tests.test_hfdev                   │
│  Role: Offline checks for the developer stack's guards and fixtures.     │
│                                                                          │
│  模块职责：离线校验开发栈的隔离约束、生产一致性与夹具逻辑。                  │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
TEAMS_CLOUD = TOOL_DIR.parents[1] / "deploy/teams-cloud"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hfdev = _load("hfdev", TOOL_DIR / "hfdev.py")
model_stub = _load("hfdev_model_stub", TOOL_DIR / "fixtures/model_stub.py")
microsoft_fixture = _load("hfdev_microsoft_fixture", TOOL_DIR / "fixtures/microsoft_fixture.py")


def _top_level_keys(text: str, section: str) -> set[str]:
    """Return two-space-indented keys under one top-level YAML section."""

    keys: set[str] = set()
    inside = False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            inside = line.rstrip() == f"{section}:"
            continue
        match = re.match(r"^  ([A-Za-z0-9_.-]+):", line)
        if inside and match:
            keys.add(match.group(1))
    return keys


def _stage(text: str, start: str) -> list[str]:
    lines = text.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(start))
    return lines[index:]


def test_dev_authority_dockerfile_matches_production_runtime_and_sources():
    production = (TEAMS_CLOUD / "Dockerfile.workspace-authority").read_text(encoding="utf-8")
    dev = (TOOL_DIR / "docker/Dockerfile.workspace-authority").read_text(encoding="utf-8")
    runtime = "FROM ${RUNTIME_BASE_IMAGE} AS runtime"
    assert _stage(dev, runtime) == _stage(production, runtime)
    copies = lambda text: [
        line for line in text.splitlines() if line.startswith("COPY Cyrene-Platform")
    ]
    assert copies(dev) == copies(production)
    targets = lambda text: re.findall(r"-p (\S+) --bin (\S+)", text)
    assert targets(dev) == targets(production)
    assert "aarch64" not in dev


def _network_blocks(text: str) -> dict[str, str]:
    """Split the top-level networks section into one text block per network."""

    section = text.split("\nnetworks:\n", 1)[1].split("\n\n", 1)[0]
    blocks: dict[str, list[str]] = {}
    current = None
    for line in section.splitlines():
        match = re.match(r"^  ([A-Za-z0-9_.-]+):$", line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def test_overlay_covers_every_network_as_internal_with_explicit_subnet():
    overlay = (TOOL_DIR / "compose.dev.yaml").read_text(encoding="utf-8")
    production = set()
    for name in ("compose.yaml", "compose.computer-runtime.yaml"):
        production |= _top_level_keys((TEAMS_CLOUD / name).read_text(encoding="utf-8"), "networks")
    blocks = _network_blocks(overlay)
    assert production == set(hfdev.NETWORKS) == set(blocks)
    for name, block in blocks.items():
        assert "    internal: true" in block, name
        assert "${HFDEV_SUBNET_" + name.upper().replace("-", "_") + ":?}" in block, name


def test_overlay_publishes_no_host_ports_and_disables_unused_services():
    overlay = (TOOL_DIR / "compose.dev.yaml").read_text(encoding="utf-8")
    assert not re.search(r"^\s+ports:", overlay, re.MULTILINE)
    for service in ("github-issues", "directory-operator", "teams-public-ingress"):
        assert re.search(rf"^  {service}:\n    profiles: \[hfdev-disabled\]", overlay, re.MULTILINE)
    assert 'CYRENE_CONVERSATION_ASSISTANT_ENABLED: "false"' in overlay


def test_port_policy_excludes_the_running_acceptance_range():
    assert set(hfdev.FORBIDDEN_PORTS) == set(range(18680, 18696))
    assert not set(hfdev.FORBIDDEN_PORTS) & set(hfdev.ALLOWED_PORTS)
    assert min(hfdev.ALLOWED_PORTS) == 18700 and max(hfdev.ALLOWED_PORTS) == 18749


def test_subnet_carving_fits_every_network():
    block = next(hfdev.NETWORK_POOL.subnets(new_prefix=hfdev.INSTANCE_PREFIX))
    assert len(list(block.subnets(new_prefix=hfdev.NETWORK_PREFIX))) >= len(hfdev.NETWORKS)


def test_identities_are_stable_uuid_fixtures():
    first, second = hfdev._identities("h0-develop"), hfdev._identities("h0-develop")
    assert first == second
    assert first != hfdev._identities("claude")
    for key in (
        "organizationId",
        "workspaceId",
        "tenantId",
        "botAppId",
        "actorOid",
        "teamsConnectionRef",
        "computerConnectionRef",
    ):
        assert str(uuid.UUID(first[key])) == first[key]
    assert first["issuer"] == f"https://login.microsoftonline.com/{first['tenantId']}/v2.0"


def test_exchange_configuration_refs_exist_in_production_compose():
    configuration = hfdev._exchange_configuration(hfdev._identities("h0"))
    compose = "\n".join(
        (TEAMS_CLOUD / name).read_text(encoding="utf-8")
        for name in ("compose.yaml", "compose.computer-runtime.yaml")
    )
    for env_name in configuration["hostSecretRefs"].values():
        assert f"{env_name}:" in compose or f"{env_name}=" in compose, env_name
    serialized = repr(configuration)
    for reference in configuration["hostSecretRefs"]:
        assert serialized.count(repr(reference)) >= 2, f"unused hostSecretRef {reference}"
    assert (
        configuration["connectorHost"]["computerRunner"]["directPlugin"]["bindingId"]
        == "computer-runtime"
    )


def _chat(messages, tools=("tool_provider_computer_computer_directory_list_0123456789ab",)):
    return {
        "model": "hfdev-stub-model",
        "stream": True,
        "messages": messages,
        "tools": [
            {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}
            for name in tools
        ],
    }


def test_model_stub_calls_directory_tool_then_quotes_its_result():
    user = {"role": "user", "content": "hfdev-nonce-0123456789abcdef please list"}
    plan, summary = model_stub.decide(_chat([user]))
    assert plan["kind"] == "tool" and summary["decision"] == "tool_call"
    assert plan["arguments"] == '{"path":"."}'
    tool_message = {"role": "tool", "tool_call_id": plan["id"], "content": '{"entries":[]}'}
    plan, summary = model_stub.decide(
        _chat([user, {"role": "assistant", "content": ""}, tool_message])
    )
    assert plan["kind"] == "final" and summary["decision"] == "final_after_tool"
    assert summary["toolResultSha256"] in plan["text"]
    assert "nonce=0123456789abcdef" in plan["text"]


def test_model_stub_flags_tool_errors_instead_of_reporting_success():
    user = {"role": "user", "content": "hfdev-nonce-0123456789abcdef please list"}
    error = {
        "role": "tool",
        "tool_call_id": "x",
        "content": "Error: Exchange connector call was rejected (400)",
    }
    plan, summary = model_stub.decide(_chat([user, error]))
    assert summary["decision"] == "final_after_tool_error"
    assert plan["text"].startswith("HFDEV-SMOKE-TOOL-ERROR")


def test_smoke_verdict_fails_when_the_tool_returned_an_error():
    result = _driver_result()
    result["positive"]["model"][1]["decision"] = "final_after_tool_error"
    verdict = hfdev.evaluate_smoke(result)
    assert not verdict["passed"] and not verdict["checks"]["toolResultIsNotError"]


def test_model_stub_answers_without_tools_when_no_nonce():
    plan, summary = model_stub.decide(_chat([{"role": "user", "content": "hello"}]))
    assert plan["kind"] == "final" and summary["decision"] == "final_without_tool"


def test_fixture_activity_route_matches_bot_connector_reply_paths():
    match = microsoft_fixture.ACTIVITY_PATH.fullmatch(
        "/amer/v3/conversations/19%3Aabc%40thread.tacv2%3Bmessageid%3D1730000000000/activities/1730000000000"
    )
    assert match and match.group("route") == "amer" and match.group("activity") == "1730000000000"
    assert microsoft_fixture.ACTIVITY_PATH.fullmatch("/amer/v3/conversations/x/activities")


def _driver_result(**reply_overrides):
    digest = "a" * 64
    reply = {
        "authorized": True,
        "conversationMatches": True,
        "text": f"HFDEV-SMOKE-OK nonce=n tool=t result_sha256={digest}",
        "textTemplate": f"HFDEV-SMOKE-OK nonce=<nonce> tool=t result_sha256={digest}",
    }
    reply.update(reply_overrides)
    return {
        "negative": {"ok": True, "cases": {"missing_authorization": {"status": 401}}},
        "positive": {
            "ackStatus": 200,
            "reply": reply,
            "model": [
                {"decision": "tool_call"},
                {"decision": "final_after_tool", "toolResultSha256": digest},
            ],
        },
    }


def test_smoke_verdict_passes_only_for_the_full_path():
    verdict = hfdev.evaluate_smoke(_driver_result())
    assert verdict["passed"] and all(verdict["checks"].values())
    assert "<sha256>" in verdict["normalized"]["replyTemplate"]
    assert not hfdev.evaluate_smoke(_driver_result(authorized=False))["passed"]
    assert not hfdev.evaluate_smoke(_driver_result(text="no digest"))["passed"]
    missing = _driver_result()
    missing["positive"]["reply"] = None
    assert not hfdev.evaluate_smoke(missing)["passed"]


def test_smoke_digest_ignores_nonce_but_not_outcome():
    assert (
        hfdev.evaluate_smoke(_driver_result())["normalizedSha256"]
        == hfdev.evaluate_smoke(_driver_result())["normalizedSha256"]
    )
    failing = _driver_result()
    failing["positive"]["ackStatus"] = 500
    assert (
        hfdev.evaluate_smoke(failing)["normalizedSha256"]
        != hfdev.evaluate_smoke(_driver_result())["normalizedSha256"]
    )


def test_overlay_header_states_legacy_default_and_base_file_deltas():
    text = (TOOL_DIR / "compose.dev.yaml").read_text(encoding="utf-8")
    header = text.split("x-hfdev-labels", 1)[0]
    assert "default work mode is legacy" in header
    assert 'default "actor"' not in header
    assert "DSH" in header
    assert "can_write_harness" in header
    assert "CYRENE_EXECUTOR_MAX_CONCURRENT_TASKS is 2" in header
    assert "depends_on" in header
    assert "H0 receipt" not in text


def test_init_refuses_leftover_volumes_with_the_volumes_command():
    with pytest.raises(hfdev.DevStackError, match="down --volumes"):
        hfdev._refuse_leftover_volumes(["cyrene-hfdev-h0_directory-postgres"])
    hfdev._refuse_leftover_volumes([])


def test_cpu_samples_alternate_quotas():
    assert hfdev._cpu_sample_order(["0.05", "0.5"], 2) == [
        ("0.05", 1),
        ("0.5", 1),
        ("0.05", 2),
        ("0.5", 2),
    ]


def test_persistence_saturation_marks_the_known_threshold():
    assert not hfdev._persistence_saturated(29, 0.59)
    assert hfdev._persistence_saturated(30, 0.1)
    assert hfdev._persistence_saturated(0, 0.60)
    assert (
        hfdev._throttle_period_ratio(
            {"nr_periods": 10, "nr_throttled": 1}, {"nr_periods": 20, "nr_throttled": 7}
        )
        == 0.6
    )


def test_smoke_evidence_includes_notification_statuses():
    source = (TOOL_DIR / "hfdev.py").read_text(encoding="utf-8")
    smoke = source.split("def cmd_smoke", 1)[1].split("\ndef ", 1)[0]
    status_at = smoke.index('summary["replyNotificationStatuses"]')
    write_at = smoke.index('_write_json(evidence / "smoke.json"')
    assert status_at < write_at


def test_pins_require_full_shas_and_known_repositories():
    assert hfdev._parse_pins(["exchange=" + "b" * 40]) == {"exchange": "b" * 40}
    for value in ("exchange=b551da2", "workspace=" + "b" * 40, "unknown=" + "b" * 40, "exchange"):
        with pytest.raises(hfdev.DevStackError):
            hfdev._parse_pins([value])
