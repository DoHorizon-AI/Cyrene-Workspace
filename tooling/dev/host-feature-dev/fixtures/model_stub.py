"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 model_stub.py                                                        │
│  Module: tooling.dev.host_feature_dev.fixtures.model_stub                │
│  Role: Deterministic OpenAI-compatible streaming model for smoke runs.   │
│                                                                          │
│  模块职责：确定性的流式模型桩——先调用一次目录工具，再引用工具结果作答。      │
└──────────────────────────────────────────────────────────────────────────┘

Navigator's ``exchange`` model source posts ``/v1/chat/completions`` with
``stream: true``. For a user turn containing ``hfdev-nonce-<16 hex>`` the stub:

1. emits one ``tool_calls`` delta for the offered computer directory-list tool
   with arguments ``{"path": "."}``;
2. after a ``tool`` message is present, emits a final answer quoting the nonce,
   the tool name, and the SHA-256 of the tool result text (``HFDEV-SMOKE-OK``, or
   ``HFDEV-SMOKE-TOOL-ERROR`` when Navigator returned an ``Error:`` tool message).

Requests without a nonce receive a short final answer. ``GET /_hfdev/requests``
returns request summaries (no bearer token). Standard library only.
中文：请求摘要只含角色、工具名与工具结果摘要，不含 bearer token。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import ssl
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

NONCE = re.compile(r"hfdev-nonce-([0-9a-f]{16})")
DIRECTORY_TOOL = re.compile(r"computer[_.]directory[_.]list", re.IGNORECASE)
MAX_BODY = 4 * 1024 * 1024
MAX_RECORDS = 1000


def _text_of(content: Any) -> str:
    """Flatten OpenAI string or content-part message bodies."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


class StubState:
    def __init__(self, token: str) -> None:
        self.token = token
        self.lock = threading.Lock()
        self.records: list[dict[str, Any]] = []

    def record(self, row: dict[str, Any]) -> None:
        with self.lock:
            row["sequence"] = len(self.records) + 1
            row["at"] = time.time()
            self.records.append(row)
            del self.records[:-MAX_RECORDS]


def decide(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (stream plan, summary) for one chat-completions request body."""

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    tool_names = [
        tool.get("function", {}).get("name", "")
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    ]
    directory_tools = [
        name
        for name, tool in zip(tool_names, tools)
        if DIRECTORY_TOOL.search(name)
        or DIRECTORY_TOOL.search(json.dumps(tool.get("function", {}).get("description", "")))
    ]
    last_user = max(
        (
            index
            for index, row in enumerate(messages)
            if isinstance(row, dict) and row.get("role") == "user"
        ),
        default=-1,
    )
    nonce_match = None
    for row in messages:
        if isinstance(row, dict) and row.get("role") == "user":
            nonce_match = NONCE.search(_text_of(row.get("content"))) or nonce_match
    nonce = nonce_match.group(1) if nonce_match else None
    tool_results = [
        _text_of(row.get("content"))
        for row in messages[last_user + 1 :]
        if isinstance(row, dict) and row.get("role") == "tool"
    ]
    summary: dict[str, Any] = {
        "model": body.get("model"),
        "stream": body.get("stream"),
        "messageCount": len(messages),
        "roles": [row.get("role") for row in messages if isinstance(row, dict)],
        "toolNames": tool_names,
        "directoryTools": directory_tools,
        "nonce": nonce,
        "toolResultCount": len(tool_results),
    }
    if nonce and tool_results:
        result = tool_results[-1]
        digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
        # Navigator hands provider/Exchange failures back as an ``Error: ...`` tool message.
        failed = result.lstrip().startswith("Error")
        summary.update(
            {
                "decision": "final_after_tool_error" if failed else "final_after_tool",
                "toolResultSha256": digest,
                "toolResultBytes": len(result.encode("utf-8")),
                "toolResultPreview": result[:2048],
            }
        )
        marker = "HFDEV-SMOKE-TOOL-ERROR" if failed else "HFDEV-SMOKE-OK"
        text = (
            f"{marker} nonce={nonce} tool={directory_tools[0] if directory_tools else 'unknown'} "
            f"result_sha256={digest}"
        )
        return {"kind": "final", "text": text}, summary
    if nonce and directory_tools:
        summary["decision"] = "tool_call"
        return {
            "kind": "tool",
            "id": f"call_hfdev_{nonce}",
            "name": directory_tools[0],
            "arguments": json.dumps({"path": "."}, separators=(",", ":")),
        }, summary
    summary["decision"] = "final_without_tool"
    return {"kind": "final", "text": f"HFDEV-STUB-FINAL nonce={nonce or 'none'}"}, summary


def _chunks(plan: dict[str, Any], model: str) -> list[dict[str, Any]]:
    base = {
        "id": "chatcmpl-hfdev",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    rows = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    ]
    if plan["kind"] == "tool":
        rows.append(
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": plan["id"],
                                    "type": "function",
                                    "function": {
                                        "name": plan["name"],
                                        "arguments": plan["arguments"],
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        rows.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    else:
        rows.append(
            {
                **base,
                "choices": [
                    {"index": 0, "delta": {"content": plan["text"]}, "finish_reason": None}
                ],
            }
        )
        rows.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    rows.append(
        {
            **base,
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    return rows


class Handler(BaseHTTPRequestHandler):
    server_version = "cyrene-hfdev-model-stub"
    protocol_version = "HTTP/1.1"
    state: StubState

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/_hfdev/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/_hfdev/requests":
            with self.state.lock:
                self._send_json(200, {"records": list(self.state.records)})
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in {"/v1/chat/completions"}:
            self._send_json(404, {"error": "not_found"})
            return
        scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
        if scheme.casefold() != "bearer" or not hmac.compare_digest(token, self.state.token):
            self.state.record({"decision": "unauthorized"})
            self._send_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send_json(413, {"error": "invalid_length"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return
        plan, summary = decide(body if isinstance(body, dict) else {})
        self.state.record(summary)
        print(
            json.dumps(
                {
                    "hfdevModelRequest": {
                        k: v for k, v in summary.items() if k != "toolResultPreview"
                    }
                }
            ),
            flush=True,
        )
        payload = (
            b"".join(
                b"data: " + json.dumps(row, separators=(",", ":")).encode() + b"\n\n"
                for row in _chunks(plan, str(body.get("model") or "hfdev-stub-model"))
            )
            + b"data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _health(port: int) -> int:
    context = ssl._create_unverified_context()  # local liveness probe only
    try:
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/_hfdev/health", context=context, timeout=3
        ) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        return _health(args.port)
    with open(args.token_file, encoding="utf-8") as stream:
        token = stream.read().strip()
    Handler.state = StubState(token)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(json.dumps({"hfdevModelStub": "listening", "port": args.port}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
