"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 teams_smoke.py                                                       │
│  Module: tooling.dev.host_feature_dev.fixtures.teams_smoke               │
│  Role: Signed Teams activity driver and readiness probe (in-network).    │
│                                                                          │
│  模块职责：在开发内网发送签名 Teams 事件、核对回复，并测量就绪时间。         │
└──────────────────────────────────────────────────────────────────────────┘

Runs inside the ``hfdev-probe`` container (Teams connector image, so PyJWT,
cryptography, grpcio, and the plugin runtime are available).

Commands:

* ``smoke`` – reject forged/unsigned activities without queuing them, then send
  one fixture-signed channel activity through ``POST /api/messages`` and wait
  for the Bot Connector fixture to record the reply;
* ``send`` – positive path only (used for first-receipt timing);
* ``wait-ready`` – poll ingress HTTP readiness and provider mTLS gRPC health;
* ``captured`` – dump fixture and model-stub ledgers;
* ``delivery-status`` – ingress durable-queue counters (no token output);
* ``navigator-task`` – read or explicitly dispatch one Navigator task (diagnostics).

Output is one JSON document on stdout. 中文：输出单个 JSON，不含任何密钥。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import json
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import jwt

CONFIG = Path("/run/secrets/hfdev-smoke-config")
SIGNING_KEY = Path("/run/secrets/hfdev-teams-fixture-signing-key")
ATTACKER_KEY = Path("/run/secrets/hfdev-teams-attacker-signing-key")
FIXTURE_CA = Path("/run/secrets/hfdev-external-fixture-root-ca")
INTERNAL_CA = Path("/run/secrets/internal-service-root-ca")
ADMIN_TOKEN = Path("/run/secrets/teams-delivery-admin-token")
NAVIGATOR_TOKEN = Path("/run/secrets/navigator-session-token")
PLUGIN_ROOT_CA = Path("/run/secrets/exchange-plugin-root-ca")
HEALTH_CERT = Path("/run/secrets/teams-connector-health-client-cert")
HEALTH_KEY = Path("/run/secrets/teams-connector-health-client-key")
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
QUEUE_COUNTS = (
    "queued_count",
    "leased_count",
    "retry_wait_count",
    "delivered_count",
    "rejected_count",
    "dead_count",
)


def _config() -> dict[str, Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _context(*cafiles: Path) -> ssl.SSLContext:
    context = ssl.create_default_context()
    for cafile in cafiles:
        context.load_verify_locations(cafile=str(cafile))
    return context


def _request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    context: ssl.SSLContext | None = None,
    timeout: float = 10,
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw.decode("utf-8", "replace")[:500]


def _token(config: dict[str, Any], key_path: Path, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": BOT_FRAMEWORK_ISSUER,
        "aud": config["botAppId"],
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "serviceurl": config["serviceUrl"],
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        key_path.read_text(encoding="utf-8"),
        algorithm="RS256",
        headers={"kid": config["jwtKeyId"]},
    )


def _activity(config: dict[str, Any], nonce: str, **overrides: Any) -> dict[str, Any]:
    message_id = str(int(time.time() * 1000) + secrets.randbelow(1000))
    bot = f"28:{config['botAppId']}"
    activity = {
        "type": "message",
        "id": message_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "serviceUrl": config["serviceUrl"],
        "channelId": "msteams",
        "from": {
            "id": "29:hfdev-fixture-user",
            "name": "HFDev Fixture User",
            "aadObjectId": config["actorOid"],
        },
        "recipient": {"id": bot, "name": "Cyrene"},
        "conversation": {
            "id": f"{config['channelId']};messageid={message_id}",
            "conversationType": "channel",
            "isGroup": True,
            "tenantId": config["tenantId"],
        },
        "channelData": {
            "team": {"id": config["teamId"]},
            "channel": {"id": config["channelId"]},
            "tenant": {"id": config["tenantId"]},
        },
        "text": f"<at>Cyrene</at> hfdev-nonce-{nonce} please list the workspace directory",
        "entities": [
            {
                "type": "mention",
                "mentioned": {"id": bot, "name": "Cyrene"},
                "text": "<at>Cyrene</at>",
            }
        ],
    }
    activity.update(overrides)
    return activity


def _post_activity(
    config: dict[str, Any], activity: dict[str, Any], token: str | None
) -> tuple[int, float]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    status, _ = _request(
        f"{config['ingressUrl']}/api/messages",
        method="POST",
        body=json.dumps(activity).encode(),
        headers=headers,
        timeout=60,
    )
    return status, time.monotonic() - started


def _delivery_status(config: dict[str, Any]) -> dict[str, Any]:
    """Read the ingress queue counters; the admin token never leaves this process."""

    token = ADMIN_TOKEN.read_text(encoding="utf-8").strip()
    status, payload = _request(
        f"{config['ingressUrl']}/_internal/teams/delivery/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"delivery status unavailable: {status}")
    return payload


def navigator_task(config: dict[str, Any], task_id: str, dispatch: bool) -> dict[str, Any]:
    """Read (or explicitly dispatch) one Navigator task; the bearer stays in-process."""

    token = NAVIGATOR_TOKEN.read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"{config.get('navigatorUrl', 'http://navigator:8080')}/api/v1/tasks/{urllib.request.quote(task_id, safe='')}"
    output: dict[str, Any] = {}
    output["status"] = _request(base + "/status", headers=headers)
    if dispatch:
        output["dispatch"] = _request(
            base + "/dispatch", method="POST", body=b"{}", headers=headers, timeout=300
        )
    return output


def _queue_total(config: dict[str, Any]) -> int:
    payload = _delivery_status(config)
    return sum(int(payload[name]) for name in QUEUE_COUNTS)


def _captured(config: dict[str, Any]) -> list[dict[str, Any]]:
    status, payload = _request(
        f"{config['fixtureUrl']}/_hfdev/captured", context=_context(FIXTURE_CA)
    )
    if status != 200:
        raise RuntimeError(f"fixture readback failed: {status}")
    return payload["records"]


def _model_requests(config: dict[str, Any]) -> list[dict[str, Any]]:
    status, payload = _request(
        f"{config['modelStubUrl']}/_hfdev/requests", context=_context(INTERNAL_CA)
    )
    if status != 200:
        raise RuntimeError(f"model stub readback failed: {status}")
    return payload["records"]


def _reply_for(records: list[dict[str, Any]], nonce: str) -> dict[str, Any] | None:
    for row in records:
        activity = row.get("activity") or {}
        if row.get("outcome") == "activity_recorded" and nonce in json.dumps(activity):
            return row
    return None


def positive(config: dict[str, Any], nonce: str, timeout: float) -> dict[str, Any]:
    """Send one signed channel activity and wait for the Bot Connector reply.

    Durations use CLOCK_MONOTONIC (shared with the host); the reply time is when this
    probe first observes the fixture record, polled every 0.1 s.
    中文：耗时均用单调时钟；回复时间为探针首次读到夹具记录的时刻（0.1 秒轮询）。
    """

    activity = _activity(config, nonce)
    sent = time.monotonic()
    status, ack_seconds = _post_activity(config, activity, _token(config, SIGNING_KEY))
    result: dict[str, Any] = {
        "nonce": nonce,
        "messageId": activity["id"],
        "sentMonotonic": sent,
        "ackStatus": status,
        "ackSeconds": round(ack_seconds, 3),
    }
    deadline = sent + timeout
    reply = None
    observed = None
    while status == 200 and time.monotonic() < deadline:
        reply = _reply_for(_captured(config), nonce)
        if reply:
            observed = time.monotonic()
            break
        time.sleep(0.1)
    result["replySeconds"] = round(observed - sent, 3) if observed else None
    if reply:
        text = (reply.get("activity") or {}).get("text", "")
        result["reply"] = {
            "authorized": reply.get("authorized"),
            "route": reply.get("route"),
            "conversationMatches": reply.get("conversationId") == activity["conversation"]["id"],
            "replyToId": reply.get("replyToId"),
            "text": text,
            "textTemplate": text.replace(nonce, "<nonce>"),
        }
    model = [row for row in _model_requests(config) if row.get("nonce") == nonce]
    result["model"] = [
        {
            key: row.get(key)
            for key in (
                "decision",
                "directoryTools",
                "toolResultCount",
                "toolResultSha256",
                "toolResultBytes",
                "toolResultPreview",
            )
        }
        for row in model
    ]
    return result


def negatives(config: dict[str, Any]) -> dict[str, Any]:
    """Forged, unsigned, mismatched, and out-of-scope activities must not be queued."""

    cases: dict[str, tuple[dict[str, Any], str | None, set[int]]] = {}
    nonce = secrets.token_hex(8)
    cases["missing_authorization"] = (_activity(config, nonce), None, {401})
    cases["attacker_signature_same_kid"] = (
        _activity(config, nonce),
        _token(config, ATTACKER_KEY),
        {401},
    )
    cases["serviceurl_claim_mismatch"] = (
        _activity(config, nonce),
        _token(config, SIGNING_KEY, serviceurl="https://smba.trafficmanager.net/emea/"),
        {401},
    )
    cases["expired_token"] = (
        _activity(config, nonce),
        _token(
            config,
            SIGNING_KEY,
            iat=int(time.time()) - 7200,
            nbf=int(time.time()) - 7200,
            exp=int(time.time()) - 3600,
        ),
        {401},
    )
    cases["wrong_audience"] = (
        _activity(config, nonce),
        _token(config, SIGNING_KEY, aud="00000000-0000-4000-8000-000000000000"),
        {401},
    )
    foreign_tenant = "11111111-1111-4111-8111-111111111111"
    foreign = _activity(config, nonce)
    foreign["channelData"]["tenant"]["id"] = foreign_tenant
    foreign["conversation"]["tenantId"] = foreign_tenant
    cases["foreign_tenant_signed"] = (foreign, _token(config, SIGNING_KEY), {200})
    before = _queue_total(config)
    replies_before = len(
        [row for row in _captured(config) if row.get("outcome") == "activity_recorded"]
    )
    results = {}
    for name, (activity, token, expected) in cases.items():
        status, _ = _post_activity(config, activity, token)
        results[name] = {"status": status, "expected": sorted(expected), "ok": status in expected}
    time.sleep(2)
    after = _queue_total(config)
    replies_after = len(
        [row for row in _captured(config) if row.get("outcome") == "activity_recorded"]
    )
    model_hits = [row for row in _model_requests(config) if row.get("nonce") == nonce]
    return {
        "cases": results,
        "queuedBefore": before,
        "queuedAfter": after,
        "notQueued": before == after,
        "noReply": replies_before == replies_after,
        "noModelCall": not model_hits,
        "ok": all(row["ok"] for row in results.values())
        and before == after
        and replies_before == replies_after
        and not model_hits,
    }


def _load_health_probe() -> Any:
    loader = importlib.machinery.SourceFileLoader(
        "hfdev_grpc_health", "/usr/local/bin/cyrene-plugin-grpc-health"
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _tcp_open(target: str) -> bool:
    host, _, port = target.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def wait_ready(
    config: dict[str, Any], roles: list[str], timeout: float, interval: float
) -> dict[str, Any]:
    """Poll Teams roles until ready; report CLOCK_MONOTONIC readiness instants.

    Prints ``{"polling": true}`` first so the host can start the container only after
    polling began. Provider readiness is mTLS gRPC Health with the expected plugin ID,
    attempted once its TCP port accepts connections.
    中文：先输出 polling 信号；provider 以 mTLS gRPC Health 返回预期插件 ID 为就绪。
    """

    probe = _load_health_probe() if "provider" in roles else None
    ingress_host = config["ingressUrl"].split("//", 1)[1]
    print(json.dumps({"polling": True}), flush=True)
    deadline = time.monotonic() + timeout
    ready: dict[str, float | None] = {role: None for role in roles}
    attempts = {role: 0 for role in roles}
    while time.monotonic() < deadline and None in ready.values():
        if "ingress" in ready and ready["ingress"] is None:
            attempts["ingress"] += 1
            if _tcp_open(ingress_host):
                try:
                    status, _ = _request(f"{config['ingressUrl']}/health/ready", timeout=2)
                    if status == 200:
                        ready["ingress"] = time.monotonic()
                except OSError:
                    pass
        if "provider" in ready and ready["provider"] is None:
            attempts["provider"] += 1
            if _tcp_open(config["providerTarget"]):
                # The port can accept connections before the runtime serves Health.
                with contextlib.suppress(Exception):
                    probe.probe(
                        config["providerTarget"],
                        "cyrene.connectors.teams",
                        PLUGIN_ROOT_CA,
                        HEALTH_CERT,
                        HEALTH_KEY,
                    )
                    ready["provider"] = time.monotonic()
        time.sleep(interval)
    return {"readyMonotonic": ready, "attempts": attempts}


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--nonce", default=None)
    smoke.add_argument("--timeout", type=float, default=240)
    smoke.add_argument("--skip-negative", action="store_true")
    send = commands.add_parser("send")
    send.add_argument("--nonce", default=None)
    send.add_argument("--timeout", type=float, default=240)
    ready = commands.add_parser("wait-ready")
    ready.add_argument("--role", action="append", choices=("ingress", "provider"), required=True)
    ready.add_argument("--timeout", type=float, default=300)
    ready.add_argument("--interval", type=float, default=0.1)
    commands.add_parser("captured")
    commands.add_parser("delivery-status")
    task = commands.add_parser("navigator-task")
    task.add_argument("--task-id", required=True)
    task.add_argument("--dispatch", action="store_true")
    args = parser.parse_args()
    config = _config()
    if args.command == "wait-ready":
        output = wait_ready(config, args.role, args.timeout, args.interval)
    elif args.command == "navigator-task":
        output = navigator_task(config, args.task_id, args.dispatch)
    elif args.command == "delivery-status":
        output = _delivery_status(config)
    elif args.command == "captured":
        output = {"fixture": _captured(config), "model": _model_requests(config)}
    else:
        nonce = args.nonce or secrets.token_hex(8)
        output = {}
        if args.command == "smoke" and not args.skip_negative:
            output["negative"] = negatives(config)
        output["positive"] = positive(config, nonce, args.timeout)
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
