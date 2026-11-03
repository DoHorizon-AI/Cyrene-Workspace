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
import json
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

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


REAL_MODEL = [
    {"decision": "tool_call"},
    {"decision": "final_after_tool", "toolResultSha256": "abc123"},
]


def _real_send(seconds=2.0):
    return {
        "positive": {
            "ackStatus": 200,
            "ackSeconds": 0.1,
            "replySeconds": seconds,
            "reply": {
                "text": "HFDEV-SMOKE-OK nonce=n result_sha256=abc123",
                "textTemplate": "HFDEV-SMOKE-OK nonce=<nonce> result_sha256=abc123",
                "authorized": True,
                "conversationMatches": True,
            },
            "model": REAL_MODEL,
        }
    }


# Navigator builds a failed task's notice as the error, a blank line, then the task output.
NOTICE_TEXT = (
    'session "teams-private-1": write ownership was lost; close this handle and reopen\n\n'
    "HFDEV-SMOKE-OK nonce=n result_sha256=abc123"
)


def _notice_send():
    positive = _real_send()["positive"]
    positive["reply"] = {
        "text": NOTICE_TEXT,
        "textTemplate": NOTICE_TEXT,
        "authorized": True,
        "conversationMatches": True,
    }
    return {"positive": positive}


def test_a_failure_notice_holding_the_answer_is_not_a_real_reply():
    assert not hfdev._reply_is_real({"reply": None, "model": REAL_MODEL})
    assert not hfdev._reply_is_real(_notice_send()["positive"])
    assert hfdev._reply_is_real(_real_send()["positive"])
    assert not hfdev.evaluate_smoke(_notice_send())["passed"]
    assert hfdev.evaluate_smoke(_real_send())["passed"]


def test_a_tool_error_digest_never_counts():
    model = [{"decision": "final_after_tool_error", "toolResultSha256": "abc123"}]
    assert hfdev._final_tool_digests(model) == []
    positive = {**_real_send()["positive"], "model": model}
    assert not hfdev._reply_is_real(positive)


def test_an_unknown_persistence_reading_counts_as_saturated():
    assert hfdev._persistence_saturated(None, 0.1)
    assert hfdev._persistence_saturated(1, None)
    assert hfdev._persistence_saturated(None, None)


def _fake_cpu_compare(monkeypatch, tmp_path, sends, *, task_count=1, throttle=0.1, mode="legacy"):
    identity = tmp_path / "identity.json"
    identity.write_text(f'{{"workMode": "{mode}"}}', encoding="utf-8")
    instance = SimpleNamespace(name="t", identity_path=identity)
    start = {
        "readySeconds": 2.0,
        "recreateCommandSeconds": 1.0,
        "cgroupAtReady": {"usage_usec": 1_000_000, "throttled_usec": 0},
    }
    queue = iter(sends)
    monkeypatch.setattr(hfdev, "_instance", lambda args: instance)
    monkeypatch.setattr(hfdev, "_evidence_run", lambda inst, label: tmp_path)
    monkeypatch.setattr(hfdev, "_cold_start", lambda *args: ("container", start))
    monkeypatch.setattr(hfdev, "_recreate", lambda *args: "container")
    monkeypatch.setattr(hfdev, "_wait_healthy", lambda *args: None)
    monkeypatch.setattr(hfdev, "_container_id", lambda *args: "container")
    monkeypatch.setattr(hfdev, "_persistence_task_count", lambda inst: task_count)
    monkeypatch.setattr(hfdev, "_persistence_idle_throttle", lambda container: throttle)
    monkeypatch.setattr(hfdev, "_cpu_limit", lambda container: 0.5)
    monkeypatch.setattr(
        hfdev,
        "_cgroup",
        lambda container: {"usage_usec": 2_000_000, "throttled_usec": 0, "memory_current": 1},
    )
    monkeypatch.setattr(hfdev, "_probe", lambda inst, *args: next(queue))
    args = SimpleNamespace(cpus=["0.5"], repeat=1)
    code = hfdev.cmd_cpu_compare(args)
    data = json.loads((tmp_path / "cpu-compare.json").read_text(encoding="utf-8"))
    return code, data["rows"][0], data["medians"]["0.5"]


def test_cpu_compare_reports_real_replies_as_a_valid_comparison(monkeypatch, tmp_path):
    code, row, medians = _fake_cpu_compare(
        monkeypatch, tmp_path, [_real_send(9.0), _real_send(2.0)]
    )
    assert code == 0
    assert row["replyValid"] is True
    assert medians["firstReplySeconds"] == 9.0
    assert medians["warmReplySeconds"] == 2.0
    assert medians["replyComparison"] == "valid"


def test_cpu_compare_does_not_count_a_failure_notice(monkeypatch, tmp_path):
    code, row, medians = _fake_cpu_compare(monkeypatch, tmp_path, [_notice_send(), _real_send()])
    assert code == 1
    assert row["firstReplySeconds"] is None
    assert row["replyValid"] is False
    assert medians["replyComparison"] == "invalid"
    for key in ("firstReplySeconds", "warmReplySeconds", "ingressFirstMessageCpuSeconds"):
        assert medians[key] is None
    assert medians["ingressReadySeconds"] == 2.0


@pytest.mark.parametrize(
    "readings",
    [
        {"task_count": 30},
        {"throttle": 0.6},
        {"task_count": None},
        {"throttle": None},
    ],
)
def test_cpu_compare_is_invalid_when_persistence_is_not_known_to_be_free(
    monkeypatch, tmp_path, readings
):
    code, _, medians = _fake_cpu_compare(
        monkeypatch, tmp_path, [_real_send(), _real_send()], **readings
    )
    assert code == 1
    assert medians["replyComparison"] == "invalid"
    assert medians["firstReplySeconds"] is None


def test_cpu_compare_refuses_private_mode(monkeypatch, tmp_path):
    with pytest.raises(hfdev.DevStackError, match="legacy"):
        _fake_cpu_compare(monkeypatch, tmp_path, [], mode="private")


def _fake_smoke(monkeypatch, tmp_path, sends, ledgers, *, repeat=1):
    instance = SimpleNamespace(name="t")
    queue = iter(sends)
    ledger_queue = iter(ledgers)

    def probe(inst, command, *args):
        if command == "captured":
            return {"fixture": [], "model": []}
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        return item

    def read_ledgers(inst):
        item = next(ledger_queue)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(hfdev, "_instance", lambda args: instance)
    monkeypatch.setattr(hfdev, "_evidence_run", lambda inst, label: tmp_path)
    monkeypatch.setattr(hfdev, "_probe", probe)
    monkeypatch.setattr(hfdev, "_ledgers", read_ledgers)
    monkeypatch.setattr(hfdev, "_save_logs", lambda *args: None)
    return SimpleNamespace(repeat=repeat, skip_negative=True, timeout=1.0)


def _ledger(*statuses, tasks=()):
    return {
        "navigator-persistence": {
            "tasks": [{"task_id": str(i), "status": s} for i, s in enumerate(tasks)],
            "notifications": [
                {"notification_id": str(i), "status": s} for i, s in enumerate(statuses)
            ],
        }
    }


def test_smoke_keeps_the_verdict_when_the_final_ledger_read_fails(monkeypatch, tmp_path):
    args = _fake_smoke(
        monkeypatch,
        tmp_path,
        [_real_send()],
        [_ledger(), hfdev.DevStackError("no ledger")],
    )
    with pytest.raises(hfdev.DevStackError):
        hfdev.cmd_smoke(args)
    summary = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))["summary"]
    assert summary["evidenceComplete"] is False
    assert summary["runs"] == 1


def test_smoke_keeps_finished_runs_when_a_later_probe_fails(monkeypatch, tmp_path):
    args = _fake_smoke(
        monkeypatch,
        tmp_path,
        [_real_send(), hfdev.DevStackError("probe failed")],
        [_ledger()],
        repeat=2,
    )
    with pytest.raises(hfdev.DevStackError):
        hfdev.cmd_smoke(args)
    summary = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))["summary"]
    assert summary["runs"] == 1
    assert summary["evidenceComplete"] is False


def test_smoke_statuses_cover_only_this_smoke(monkeypatch, tmp_path):
    args = _fake_smoke(
        monkeypatch,
        tmp_path,
        [_real_send()],
        [
            _ledger("failed", "uncertain", tasks=("failed",)),
            _ledger("failed", "uncertain", "delivered", "delivered", tasks=("failed", "completed")),
        ],
    )
    assert hfdev.cmd_smoke(args) == 0
    summary = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))["summary"]
    assert summary["replyNotificationStatuses"] == ["delivered", "delivered"]
    assert summary["tasksCompleted"] is True
    assert summary["evidenceComplete"] is True


def test_smoke_fails_on_a_failing_verdict(monkeypatch, tmp_path):
    args = _fake_smoke(
        monkeypatch,
        tmp_path,
        [_notice_send()],
        [_ledger(), _ledger("delivered", tasks=("completed",))],
    )
    assert hfdev.cmd_smoke(args) == 1
    summary = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))["summary"]
    assert summary["allPassed"] is False


def test_smoke_fails_when_runs_differ(monkeypatch, tmp_path):
    other = _real_send()
    other["positive"]["ackStatus"] = 201
    args = _fake_smoke(
        monkeypatch,
        tmp_path,
        [_real_send(), other],
        [_ledger(), _ledger("delivered", tasks=("completed", "completed"))],
        repeat=2,
    )
    assert hfdev.cmd_smoke(args) == 1


def test_smoke_fails_when_its_task_failed_despite_a_clean_reply(monkeypatch, tmp_path):
    args = _fake_smoke(
        monkeypatch,
        tmp_path,
        [_real_send()],
        [_ledger(), _ledger("delivered", tasks=("failed",))],
    )
    assert hfdev.cmd_smoke(args) == 1
    summary = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))["summary"]
    assert summary["allPassed"] is True
    assert summary["tasksCompleted"] is False


def test_smoke_reports_unknown_scope_when_the_first_ledger_read_fails(monkeypatch, tmp_path):
    args = _fake_smoke(
        monkeypatch,
        tmp_path,
        [_real_send()],
        [hfdev.DevStackError("no ledger"), _ledger("delivered", tasks=("completed",))],
    )
    assert hfdev.cmd_smoke(args) == 1
    summary = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))["summary"]
    assert summary["replyNotificationStatuses"] is None
    assert summary["tasksCompleted"] is None
    assert summary["evidenceComplete"] is False


def test_task_count_is_unknown_when_the_ledger_cannot_be_read(monkeypatch):
    def fail(instance):
        raise hfdev.DevStackError("no ledger")

    monkeypatch.setattr(hfdev, "_ledgers", fail)
    assert hfdev._persistence_task_count(SimpleNamespace()) is None


def test_idle_throttle_samples_the_configured_window(monkeypatch):
    slept = []
    samples = iter([{"nr_periods": 10, "nr_throttled": 1}, {"nr_periods": 20, "nr_throttled": 6}])
    monkeypatch.setattr(hfdev, "_cgroup", lambda container: next(samples))
    monkeypatch.setattr(hfdev.time, "sleep", slept.append)
    assert hfdev._persistence_idle_throttle("container") == 0.5
    assert slept == [hfdev.PERSISTENCE_IDLE_SAMPLE_SECONDS]


def test_cpu_compare_with_no_samples_is_not_a_success(monkeypatch, tmp_path):
    identity = tmp_path / "identity.json"
    identity.write_text('{"workMode": "legacy"}', encoding="utf-8")
    instance = SimpleNamespace(name="t", identity_path=identity)
    monkeypatch.setattr(hfdev, "_instance", lambda args: instance)
    monkeypatch.setattr(hfdev, "_evidence_run", lambda inst, label: tmp_path)
    monkeypatch.setattr(hfdev, "_cold_start", lambda *args: ("container", {}))
    monkeypatch.setattr(hfdev, "_recreate", lambda *args: "container")
    monkeypatch.setattr(hfdev, "_wait_healthy", lambda *args: None)
    assert hfdev.cmd_cpu_compare(SimpleNamespace(cpus=["0.5"], repeat=0)) == 1


def _fake_measure(monkeypatch, tmp_path, task_count):
    monkeypatch.setattr(hfdev, "_instance", lambda args: SimpleNamespace(name="t"))
    monkeypatch.setattr(hfdev, "_evidence_run", lambda inst, label: tmp_path)
    monkeypatch.setattr(hfdev, "_project_containers", lambda *a, **k: [{"ID": "persistence"}])
    monkeypatch.setattr(
        hfdev,
        "_inspect_container",
        lambda container: {
            "Config": {
                "Labels": {"com.docker.compose.service": "navigator-persistence"},
                "Image": "image",
            },
            "Image": "sha256:image",
            "HostConfig": {"NanoCpus": 100_000_000, "Memory": 1},
            "State": {},
        },
    )
    monkeypatch.setattr(hfdev, "_inspect_image", lambda image: {"Size": 1})
    samples = iter(
        [
            {"usage_usec": 0, "nr_periods": 0, "nr_throttled": 0, "memory_current": 1},
            {"usage_usec": 1, "nr_periods": 10, "nr_throttled": 1, "memory_current": 1},
        ]
    )
    monkeypatch.setattr(hfdev, "_cgroup", lambda container: next(samples))
    monkeypatch.setattr(hfdev, "_persistence_task_count", lambda inst: task_count)
    monkeypatch.setattr(hfdev.time, "sleep", lambda seconds: None)
    assert hfdev.cmd_measure(SimpleNamespace(window=1)) == 0
    return json.loads((tmp_path / "measure.json").read_text(encoding="utf-8"))["summary"]


def test_measure_marks_a_clean_reading_comparable(monkeypatch, tmp_path):
    summary = _fake_measure(monkeypatch, tmp_path, 3)
    assert summary["persistenceSaturated"] is False
    assert summary["idleCpuComparable"] is True


def test_measure_marks_an_unknown_reading_not_comparable(monkeypatch, tmp_path):
    summary = _fake_measure(monkeypatch, tmp_path, None)
    assert summary["persistenceTaskCount"] is None
    assert summary["idleCpuComparable"] is False


def test_main_reports_a_timeout_instead_of_a_traceback(monkeypatch, capsys):
    def time_out(args):
        raise hfdev.subprocess.TimeoutExpired("docker", 5)

    monkeypatch.setattr(hfdev, "cmd_status", time_out)
    assert hfdev.main(["status"]) == 2
    assert "timed out" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("mode", "personal", "actor_subject"),
    [("legacy", False, False), ("actor", False, True), ("private", True, True)],
)
def test_compose_environment_enables_personal_delivery_only_in_private_mode(
    tmp_path, mode, personal, actor_subject
):
    ids = hfdev._identities("h0")
    instance = SimpleNamespace(
        name="h0",
        project="cyrene-hfdev-h0",
        config_dir=tmp_path,
        secrets_dir=tmp_path,
        runtime_dir=tmp_path,
    )
    lock = {
        "images": {"teams": {"composeEnv": "CYRENE_IMAGE_TEAMS_INGRESS", "image": "image"}},
        "externalImages": {"postgres": {"image": "postgres"}},
    }
    subnets = {name: "10.249.0.0/26" for name in hfdev.NETWORKS}
    environment = hfdev._compose_environment(instance, lock, ids, subnets, "fingerprint", mode)
    assert environment["CYRENE_TEAMS_PERSONAL_CHAT_ENABLED"] == ("true" if personal else "false")
    assert environment["CYRENE_TEAMS_MODEL_ACTOR_SUBJECT"] == (
        ids["actorOid"] if actor_subject else ""
    )
    secret_map = environment["HFDEV_NAVIGATOR_SECRET_FILE_MAP"]
    assert ("CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN" in secret_map) is actor_subject


def _installation_db(path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE work_private_installations (organization_id TEXT, workspace_id TEXT, "
        "tenant_id TEXT, actor_aad_object_id TEXT, bot_app_id TEXT, active INTEGER)"
    )
    connection.executemany("INSERT INTO work_private_installations VALUES (?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    ("active", "matching", "expected"),
    [(1, True, True), (0, True, False), (1, False, False)],
)
def test_private_installation_check_reads_the_exact_row(
    monkeypatch, tmp_path, active, matching, expected
):
    ids = hfdev._identities("h0")
    database = tmp_path / "persistence.db"
    actor = ids["actorOid"].lower() if matching else "someone-else"
    _installation_db(
        database,
        [
            (
                ids["organizationId"],
                ids["workspaceId"],
                ids["tenantId"].lower(),
                actor,
                ids["botAppId"].lower(),
                active,
            )
        ],
    )

    def run_against_temp_database(argv, capture=False):
        script = argv[argv.index("-c") + 1].replace(
            "/var/lib/cyrene-navigator/persistence.db", str(database)
        )
        return subprocess.run(
            [sys.executable, "-c", script, *argv[argv.index("-c") + 2 :]],
            capture_output=True,
            check=True,
        )

    monkeypatch.setattr(hfdev, "_run", run_against_temp_database)
    monkeypatch.setattr(hfdev, "_container_id", lambda *args: "container")
    monkeypatch.setattr(hfdev.time, "sleep", lambda seconds: None)
    assert hfdev._private_installation_active(SimpleNamespace(), ids) is expected


def test_compose_files_add_the_actor_overlay_only_for_actor_modes(tmp_path):
    identity = tmp_path / "identity.json"
    for mode, expected in (("legacy", False), ("actor", True), ("private", True)):
        identity.write_text(f'{{"workMode": "{mode}"}}', encoding="utf-8")
        files = hfdev._compose_files(SimpleNamespace(identity_path=identity))
        assert (hfdev.ACTOR_WORK_COMPOSE in files) is expected


@pytest.fixture
def smoke_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, "jwt", SimpleNamespace(encode=lambda *a, **k: "token"))
    driver = _load("teams_smoke", TOOL_DIR / "fixtures/teams_smoke.py")
    return driver


SMOKE_CONFIG = {
    "botAppId": "bot",
    "tenantId": "tenant",
    "teamId": "team",
    "channelId": "19:channel",
    "personalConversationId": "a:personal",
    "actorOid": "actor",
    "serviceUrl": "https://example.invalid/",
    "jwtKeyId": "key",
}


def test_private_activity_is_personal_and_channel_activity_is_unchanged(smoke_driver):
    channel = smoke_driver._activity(SMOKE_CONFIG, "n")
    assert channel["conversation"]["conversationType"] == "channel"
    assert channel["channelData"]["team"] == {"id": "team"}
    private = smoke_driver._activity({**SMOKE_CONFIG, "workMode": "private"}, "n")
    assert private["conversation"] == {
        "id": "a:personal",
        "conversationType": "personal",
        "isGroup": False,
        "tenantId": "tenant",
    }
    assert "team" not in private["channelData"]
    assert private["entities"] == []


def test_private_negatives_add_install_cases_and_channel_ones_do_not(smoke_driver, monkeypatch):
    monkeypatch.setattr(smoke_driver, "_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(smoke_driver, "_queue_total", lambda config: 0)
    monkeypatch.setattr(smoke_driver, "_captured", lambda config: [])
    monkeypatch.setattr(smoke_driver, "_model_requests", lambda config: [])
    monkeypatch.setattr(smoke_driver.time, "sleep", lambda seconds: None)
    posted = []

    def post(config, activity, token):
        posted.append(activity)
        return (401 if token is None else 200, 0.1)

    monkeypatch.setattr(smoke_driver, "_post_activity", post)
    channel = smoke_driver.negatives(SMOKE_CONFIG)
    assert "unsigned_installation" not in channel["cases"]
    private = smoke_driver.negatives({**SMOKE_CONFIG, "workMode": "private"})
    assert private["cases"]["unsigned_installation"]["status"] == 401
    foreign = next(
        a
        for a in posted
        if a["type"] == "installationUpdate" and a["channelData"]["tenant"]["id"] != "tenant"
    )
    assert foreign["conversation"]["tenantId"] == foreign["channelData"]["tenant"]["id"]


def _counts(delivered=0, rejected=0, dead=0):
    return {
        "queued_count": 0,
        "leased_count": 0,
        "retry_wait_count": 0,
        "delivered_count": delivered,
        "rejected_count": rejected,
        "dead_count": dead,
    }


@pytest.mark.parametrize(
    ("after", "delivered", "refused"),
    [
        (_counts(delivered=1), True, False),
        (_counts(rejected=1), False, True),
        (_counts(dead=1), False, True),
    ],
)
def test_install_reports_what_ingress_did_without_waiting_out_the_timeout(
    smoke_driver, monkeypatch, after, delivered, refused
):
    states = iter([_counts(), after, after])
    monkeypatch.setattr(smoke_driver, "_delivery_status", lambda config: next(states))
    monkeypatch.setattr(smoke_driver, "_post_activity", lambda *args: (200, 0.1))
    monkeypatch.setattr(smoke_driver, "_token", lambda *args, **kwargs: "token")
    result = smoke_driver.install(SMOKE_CONFIG, 60)
    assert result["delivered"] is delivered
    assert result.get("refused", False) is refused


def test_private_mode_uses_the_actor_overlay_and_names_the_owner_in_exchange():
    ids = hfdev._identities("h0")
    assert "private" in hfdev.ACTOR_WORK_MODES
    private = hfdev._exchange_configuration(ids, "private")
    legacy = hfdev._exchange_configuration(ids)
    assert private["connectorHost"]["teamsMessages"]["personalActorSubjects"] == [ids["actorOid"]]
    assert "personalActorSubjects" not in legacy["connectorHost"]["teamsMessages"]
    assert ids["personalConversationId"].startswith("a:")


def test_run_refuses_leftover_volumes_before_build(monkeypatch):
    monkeypatch.setattr(hfdev, "_instance", lambda args: "instance")
    monkeypatch.setattr(hfdev, "_project_volumes", lambda instance: ["leftover"])

    def fail_if_called(argv):
        raise AssertionError(f"run reached a step after refusing: {argv}")

    monkeypatch.setattr(hfdev, "main", fail_if_called)
    args = SimpleNamespace(instance="h0", state_root="/unused", pin=[], work_mode="legacy")
    with pytest.raises(hfdev.DevStackError, match="down --volumes"):
        hfdev.cmd_run(args)


def test_pins_require_full_shas_and_known_repositories():
    assert hfdev._parse_pins(["exchange=" + "b" * 40]) == {"exchange": "b" * 40}
    for value in ("exchange=b551da2", "workspace=" + "b" * 40, "unknown=" + "b" * 40, "exchange"):
        with pytest.raises(hfdev.DevStackError):
            hfdev._parse_pins([value])
