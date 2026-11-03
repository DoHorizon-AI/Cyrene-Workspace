"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 microsoft_fixture.py                                                 │
│  Module: tooling.dev.host_feature_dev.fixtures.microsoft_fixture         │
│  Role: Local stand-in for the three Microsoft endpoints Teams calls.     │
│                                                                          │
│  模块职责：在开发内网替代 Bot Framework JWKS、令牌与 Bot Connector 端点。   │
└──────────────────────────────────────────────────────────────────────────┘

The fixture is reachable only through private Docker aliases
(login.botframework.com, login.microsoftonline.com, smba.trafficmanager.net)
and serves TLS from a locally generated fixture CA. It answers:

* Bot Framework OpenID metadata and JWKS for inbound activity tokens;
* Entra tenant discovery and the client-credentials token endpoint, checking
  the fixture bot app ID and secret;
* Bot Connector conversation activity create/update, recording each body.

``GET /_hfdev/captured`` returns recorded requests for the smoke driver. Every
other path is answered 404 and recorded as unexpected. Standard library only.
中文：只用于开发夹具；记录每次访问，未知路径返回 404 并标记为 unexpected。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
BOT_FRAMEWORK_KEYS = "https://login.botframework.com/v1/.well-known/keys"
TENANT_PATH = re.compile(
    r"^/(?P<tenant>[A-Za-z0-9.-]{1,128})/v2\.0/\.well-known/openid-configuration$"
)
TOKEN_PATH = re.compile(r"^/(?P<tenant>[A-Za-z0-9.-]{1,128})/oauth2/v2\.0/token$")
ACTIVITY_PATH = re.compile(
    r"^/(?P<route>[A-Za-z0-9-]{1,128}(?:/[A-Za-z0-9-]{1,128})?)/v3/conversations/"
    r"(?P<conversation>[^/]+)/activities(?:/(?P<activity>[^/]+))?$"
)
MAX_BODY = 1024 * 1024
MAX_RECORDS = 2000


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class FixtureState:
    """Thread-safe request ledger and issued bot tokens."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.lock = threading.Lock()
        self.records: list[dict[str, Any]] = []
        self.issued_tokens: set[str] = set()
        self.activity_sequence = int(time.time() * 1000)
        self._token_key = secrets.token_bytes(32)

    def record(self, row: dict[str, Any]) -> None:
        with self.lock:
            row["sequence"] = len(self.records) + 1
            row["at"] = time.time()
            self.records.append(row)
            del self.records[:-MAX_RECORDS]

    def issue_token(self, tenant: str) -> str:
        """Return a well-formed, fixture-only JWT the SDK can parse but nobody can reuse."""

        now = int(time.time())
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        claims = {
            "aud": BOT_FRAMEWORK_ISSUER,
            "iss": f"https://sts.windows.net/{tenant}/",
            "appid": self.config["botAppId"],
            "tid": self.config["tenantId"],
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "hfdev": "fixture-token",
        }
        payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
        signature = hmac.new(self._token_key, f"{header}.{payload}".encode(), hashlib.sha256)
        token = f"{header}.{payload}.{_b64url(signature.digest())}"
        with self.lock:
            self.issued_tokens.add(token)
        return token

    def token_is_issued(self, token: str) -> bool:
        with self.lock:
            return token in self.issued_tokens

    def next_activity_id(self) -> str:
        with self.lock:
            self.activity_sequence += 1
            return str(self.activity_sequence)


class Handler(BaseHTTPRequestHandler):
    """Route the fixed Microsoft endpoint paths; everything else is 404."""

    server_version = "cyrene-hfdev-microsoft-fixture"
    protocol_version = "HTTP/1.1"
    state: FixtureState

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _host(self) -> str:
        return (self.headers.get("Host") or "").split(":", 1)[0].lower()

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length) if length else b""

    def _summary(self, outcome: str, **extra: Any) -> None:
        row = {
            "method": self.command,
            "host": self._host(),
            "path": urllib.parse.urlsplit(self.path).path,
            "outcome": outcome,
        }
        row.update(extra)
        print(json.dumps({"hfdevFixtureRequest": row}, sort_keys=True), flush=True)
        self.state.record(row)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        host = self._host()
        if path == "/_hfdev/health":
            self._send(200, {"status": "ok"})
            return
        if path == "/_hfdev/captured":
            with self.state.lock:
                records = list(self.state.records)
            self._send(200, {"records": records})
            return
        if host == "login.botframework.com" and path == "/v1/.well-known/openidconfiguration":
            self._summary("openid_metadata")
            self._send(
                200,
                {
                    "issuer": BOT_FRAMEWORK_ISSUER,
                    "authorization_endpoint": "https://invalid.botframework.com",
                    "jwks_uri": BOT_FRAMEWORK_KEYS,
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "token_endpoint_auth_methods_supported": ["private_key_jwt"],
                },
            )
            return
        if host == "login.botframework.com" and path == "/v1/.well-known/keys":
            self._summary("jwks")
            self._send(200, self.state.config["jwks"])
            return
        if host == "login.microsoftonline.com" and path == "/common/discovery/instance":
            tenant = self.state.config["tenantId"]
            self._summary("instance_discovery")
            self._send(
                200,
                {
                    "tenant_discovery_endpoint": f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration",
                    "api-version": "1.1",
                    "metadata": [
                        {
                            "preferred_network": "login.microsoftonline.com",
                            "preferred_cache": "login.windows.net",
                            "aliases": ["login.microsoftonline.com"],
                        }
                    ],
                },
            )
            return
        match = TENANT_PATH.fullmatch(path)
        if host == "login.microsoftonline.com" and match:
            tenant = match.group("tenant")
            base = f"https://login.microsoftonline.com/{tenant}"
            self._summary("tenant_discovery", tenant=tenant)
            self._send(
                200,
                {
                    "issuer": f"https://login.microsoftonline.com/{self.state.config['tenantId']}/v2.0",
                    "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
                    "token_endpoint": f"{base}/oauth2/v2.0/token",
                    "jwks_uri": f"{base}/discovery/v2.0/keys",
                    "response_types_supported": ["code", "id_token", "token"],
                    "subject_types_supported": ["pairwise"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_post"],
                },
            )
            return
        self._summary("unexpected")
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        host = self._host()
        try:
            body = self._body()
        except ValueError:
            self._summary("body_rejected")
            self._send(413, {"error": "too_large"})
            return
        match = TOKEN_PATH.fullmatch(path)
        if host == "login.microsoftonline.com" and match:
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            client_id = (form.get("client_id") or [""])[0]
            client_secret = (form.get("client_secret") or [""])[0]
            grant_type = (form.get("grant_type") or [""])[0]
            valid = (
                grant_type == "client_credentials"
                and hmac.compare_digest(client_id, self.state.config["botAppId"])
                and hmac.compare_digest(client_secret, self.state.config["clientSecret"])
            )
            self._summary(
                "token_issued" if valid else "token_rejected",
                tenant=match.group("tenant"),
                scope=(form.get("scope") or [""])[0],
            )
            if not valid:
                self._send(401, {"error": "invalid_client"})
                return
            token = self.state.issue_token(match.group("tenant"))
            self._send(
                200,
                {
                    "token_type": "Bearer",
                    "expires_in": 3599,
                    "ext_expires_in": 3599,
                    "access_token": token,
                },
            )
            return
        self._activity(host, path, body)

    def do_PUT(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self._body()
        except ValueError:
            self._send(413, {"error": "too_large"})
            return
        self._activity(self._host(), path, body)

    def _activity(self, host: str, path: str, body: bytes) -> None:
        match = ACTIVITY_PATH.fullmatch(path)
        if host != "smba.trafficmanager.net" or not match:
            self._summary("unexpected")
            self._send(404, {"error": "not_found"})
            return
        authorization = self.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        authorized = scheme.casefold() == "bearer" and self.state.token_is_issued(token)
        try:
            activity = json.loads(body or b"{}")
        except json.JSONDecodeError:
            activity = None
        activity_id = (
            match.group("activity") if self.command == "PUT" else self.state.next_activity_id()
        )
        self._summary(
            "activity_recorded" if authorized else "activity_unauthorized",
            route=match.group("route"),
            conversationId=urllib.parse.unquote(match.group("conversation")),
            replyToId=match.group("activity"),
            authorized=authorized,
            activity=activity,
            responseId=activity_id,
        )
        if not authorized:
            self._send(401, {"error": {"code": "Unauthorized"}})
            return
        self._send(200 if self.command == "PUT" else 201, {"id": activity_id})


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
    parser.add_argument("--config")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        return _health(args.port)
    with open(args.config, encoding="utf-8") as stream:
        config = json.load(stream)
    Handler.state = FixtureState(config)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(json.dumps({"hfdevFixture": "listening", "port": args.port}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
