"""
Cyrene Exchange Product server for local acceptance testing.
Combines control plane (endpoints, routes, drafts) and data plane (/v1/chat/completions).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from cyrene_exchange_product.api import create_app
from cyrene_exchange_product.domain import ProductPrincipal


def build_exchange_app(
    database: Path,
    control_token: str,
    actor_id: str,
    workspace_id: str,
    allowed_bindings: frozenset[str],
) -> FastAPI:
    database.parent.mkdir(parents=True, exist_ok=True)
    principal = ProductPrincipal(
        actor_id=actor_id,
        workspace_id=workspace_id,
        credential_ref="cred://exchange/acceptance-control",
    )
    control_credentials = {control_token: principal}

    app = create_app(
        database_path=database,
        control_credentials=control_credentials,
        allowed_binding_ids=allowed_bindings,
        validate_route_target=lambda _route: None,
    )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        body = await request.json()
        model_name = body.get("model")
        if not model_name:
            raise HTTPException(status_code=400, detail="model is required")

        conn = sqlite3.connect(str(database))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT document FROM gateway_routes WHERE state = 'ACTIVE' ORDER BY priority DESC"
            ).fetchall()
        finally:
            conn.close()

        target_route: dict[str, Any] | None = None
        for row in rows:
            doc = json.loads(row["document"])
            pattern = doc.get("modelPattern") or doc.get("model_pattern")
            if pattern == model_name or pattern == "*":
                target_route = doc
                break

        if target_route is None:
            raise HTTPException(status_code=404, detail=f"No active route matching model '{model_name}'")

        source = target_route.get("source") or {}
        resource_uri = source.get("resourceUri") or source.get("resource_uri")
        target_model = target_route.get("targetModel") or target_route.get("target_model")

        if not resource_uri:
            raise HTTPException(status_code=502, detail="Route source resourceUri is missing")

        async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
            try:
                ep_resp = await client.get(resource_uri)
                ep_resp.raise_for_status()
                ep_data = ep_resp.json()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Failed to fetch endpoint from {resource_uri}: {exc}") from exc

            endpoint_url = ep_data.get("url")
            if not endpoint_url:
                raise HTTPException(status_code=502, detail="Target endpoint has no URL")

            upstream_url = endpoint_url.rstrip("/") + "/chat/completions"
            forward_body = dict(body)
            if target_model:
                forward_body["model"] = target_model

            try:
                resp = await client.post(upstream_url, json=forward_body)
                return JSONResponse(status_code=resp.status_code, content=resp.json())
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Failed to forward request to {upstream_url}: {exc}") from exc

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Cyrene Exchange for acceptance testing")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--control-token", required=True)
    parser.add_argument("--actor-id", default="acceptance-operator")
    parser.add_argument("--workspace-id", default="acceptance-workspace")
    parser.add_argument("--allowed-bindings", default="local-gpu,vllm-product")
    args = parser.parse_args()

    allowed = frozenset(b.strip() for b in args.allowed_bindings.split(",") if b.strip())
    app = build_exchange_app(
        database=args.database.resolve(),
        control_token=args.control_token,
        actor_id=args.actor_id,
        workspace_id=args.workspace_id,
        allowed_bindings=allowed,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
