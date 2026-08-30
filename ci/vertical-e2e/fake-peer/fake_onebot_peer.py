#!/usr/bin/env python3
"""Deterministic OneBot v11 forward-WebSocket peer for the CI vertical slice.

The process is an external protocol counterpart, not a connector double. The
real official connector opens the WebSocket, receives the message event, and
sends the action. This peer only implements the small OneBot v11 wire surface
needed by the acceptance scenario and records every frame in JSONL for
post-run assertions.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import signal
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SHARED_CONVERSATION_ID = 424242
SHARED_MESSAGE_ID = 90001


class PeerState:
    def __init__(
        self,
        *,
        binding_id: str,
        account_id: str,
        access_token: str,
        state_file: Path,
    ) -> None:
        self.binding_id = binding_id
        self.account_id = account_id
        self.access_token = access_token
        self.state_file = state_file
        self.stop = threading.Event()
        self.write_lock = threading.Lock()
        self.event_sent = False
        self.connections = 0

    def record(self, kind: str, **values: Any) -> None:
        record = {
            "kind": kind,
            "binding_id": self.binding_id,
            "account_id": self.account_id,
            "time": time.time(),
            **values,
        }
        with self.write_lock:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with self.state_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()

    def message_event(self) -> dict[str, Any]:
        return {
            "time": int(time.time()),
            "self_id": int(self.account_id),
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": SHARED_MESSAGE_ID,
            "user_id": SHARED_CONVERSATION_ID,
            "message": [
                {
                    "type": "text",
                    "data": {"text": f"/phase7 {self.binding_id}"},
                }
            ],
            "raw_message": f"/phase7 {self.binding_id}",
            "sender": {
                "user_id": SHARED_CONVERSATION_ID,
                "nickname": "phase7-user",
            },
        }


class ThreadedPeer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: PeerState):
        self.state = state
        super().__init__(address, PeerHandler)


class PeerHandler(socketserver.BaseRequestHandler):
    server: ThreadedPeer

    def handle(self) -> None:
        state = self.server.state
        connection = self.request
        connection.settimeout(0.25)
        state.connections += 1
        try:
            request_line, headers, remainder = read_http_request(connection)
            if not request_line.startswith("GET ") or headers.get("upgrade", "").lower() != "websocket":
                send_http_error(connection, 400, "Bad Request")
                state.record("handshake_rejected", reason="invalid_request")
                return
            if not authorized(headers, state.access_token):
                send_http_error(connection, 401, "Unauthorized")
                state.record("handshake_rejected", reason="authentication_failed")
                return
            key = headers.get("sec-websocket-key")
            if not key:
                send_http_error(connection, 400, "Bad Request")
                state.record("handshake_rejected", reason="missing_key")
                return
            accept = base64.b64encode(
                hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
            ).decode("ascii")
            connection.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii")
            )
            state.record("connected", connection_number=state.connections)
            buffer = bytearray(remainder)
            if not state.event_sent:
                time.sleep(0.05)
                heartbeat = {
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "self_id": int(state.account_id),
                    "interval": 1000,
                }
                send_json(connection, heartbeat)
                state.record("heartbeat", event=heartbeat)
                event = state.message_event()
                send_json(connection, event)
                state.event_sent = True
                state.record("event", event=event)

            while not state.stop.is_set():
                try:
                    chunk = connection.recv(64 * 1024)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buffer.extend(chunk)
                while True:
                    frame = take_frame(buffer)
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode == 0x8:
                        try:
                            send_frame(connection, 0x8, payload[:125])
                        except OSError:
                            pass
                        return
                    if opcode == 0x9:
                        send_frame(connection, 0xA, payload)
                        continue
                    if opcode != 0x1:
                        state.record("protocol_error", opcode=opcode)
                        return
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        state.record("protocol_error", reason="invalid_json")
                        return
                    if not isinstance(message, dict):
                        state.record("protocol_error", reason="non_object_json")
                        return
                    if "action" not in message or "echo" not in message:
                        state.record("frame", message=message)
                        continue
                    action = str(message["action"])
                    params = message.get("params", {})
                    state.record(
                        "action",
                        action=action,
                        echo=str(message["echo"]),
                        params=params,
                    )
                    if action not in {"send_private_msg", "send_group_msg"}:
                        response = {
                            "status": "failed",
                            "retcode": 1404,
                            "data": {},
                            "echo": message["echo"],
                        }
                    else:
                        response = {
                            "status": "ok",
                            "retcode": 0,
                            "data": {"message_id": 90002},
                            "echo": message["echo"],
                        }
                    send_json(connection, response)
                    state.record("action_response", response=response)
        except (ConnectionError, OSError):
            return
        finally:
            try:
                connection.close()
            except OSError:
                pass


def read_http_request(connection: socket.socket) -> tuple[str, dict[str, str], bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) > 64 * 1024:
            raise ConnectionError("WebSocket handshake too large")
        chunk = connection.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed during handshake")
        data.extend(chunk)
    head, remainder = bytes(data).split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers, remainder


def authorized(headers: dict[str, str], expected: str) -> bool:
    return not expected or headers.get("authorization", "") == f"Bearer {expected}"


def send_http_error(connection: socket.socket, status: int, reason: str) -> None:
    connection.sendall(
        f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode(
            "ascii"
        )
    )


def send_json(connection: socket.socket, value: dict[str, Any]) -> None:
    send_frame(
        connection,
        0x1,
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


def send_frame(connection: socket.socket, opcode: int, payload: bytes) -> None:
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, length))
    elif length < 65_536:
        header = bytes((0x80 | opcode, 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((0x80 | opcode, 127)) + length.to_bytes(8, "big")
    connection.sendall(header + payload)


def take_frame(buffer: bytearray) -> tuple[int, bytes] | None:
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    opcode = first & 0x0F
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < offset + 2:
            return None
        length = int.from_bytes(buffer[offset : offset + 2], "big")
        offset += 2
    elif length == 127:
        if len(buffer) < offset + 8:
            return None
        length = int.from_bytes(buffer[offset : offset + 8], "big")
        offset += 8
    if length > 16 * 1024 * 1024:
        raise ConnectionError("WebSocket frame too large")
    masked = bool(second & 0x80)
    if masked:
        if len(buffer) < offset + 4:
            return None
        mask = bytes(buffer[offset : offset + 4])
        offset += 4
    else:
        mask = b""
    end = offset + length
    if len(buffer) < end:
        return None
    payload = bytearray(buffer[offset:end])
    del buffer[:end]
    if masked:
        for index in range(length):
            payload[index] ^= mask[index % 4]
    return opcode, bytes(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = PeerState(
        binding_id=args.binding_id,
        account_id=args.account_id,
        access_token=args.access_token,
        state_file=args.state_file,
    )
    server = ThreadedPeer((args.host, args.port), state)
    host, port = server.server_address
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    args.ready_file.write_text(f"{host}:{port}\n", encoding="utf-8")
    state.record("ready", host=host, port=port)

    def stop(_signum: int, _frame: Any) -> None:
        state.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        state.stop.set()
        server.server_close()
        state.record("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
