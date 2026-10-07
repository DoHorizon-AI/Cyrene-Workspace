#!/usr/bin/env python3
"""Run the existing Navigator Web Host with fixed Product proxy targets.

The trial passes the API bearer from this process environment into each
server-owned ProxyTarget; browser requests never receive the bearer value.
使用服务端 ProxyTarget 注入 API bearer，浏览器不会接触该密钥。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Load the canonical Navigator host and serve its same-origin API proxy."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--navigator-root", type=Path, required=True)
    parser.add_argument("--pairing-code-file", type=Path, required=True)
    parser.add_argument("--catalyst-url", required=True)
    parser.add_argument("--echo-url")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)

    source = args.navigator_root.expanduser().resolve() / "src"
    if not source.is_dir():
        parser.error(f"Navigator source directory is missing: {source}")
    sys.path.insert(0, str(source))
    try:
        import uvicorn
        from cyrene_navigator.web_host import ProxyTarget, create_web_host_app
    except ImportError as error:
        parser.error(f"Navigator Web Host runtime dependencies are unavailable: {error}")

    code_path = args.pairing_code_file.expanduser().absolute()
    if code_path.is_symlink() or not code_path.is_file():
        parser.error("pairing code file is missing or unsafe")
    try:
        pairing_code = code_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        parser.error(f"could not read pairing code file: {error}")
    finally:
        code_path.unlink(missing_ok=True)
    if not pairing_code:
        parser.error("pairing code file is empty")

    token = os.environ.get("CYRENE_DATA_TOOLS_TOKEN")
    proxy_targets = {
        "/api/v1/catalyst/api/v1": ProxyTarget(
            f"{args.catalyst_url.rstrip('/')}/api/v1", bearer_token=token
        ),
        "/api/v1/catalyst": ProxyTarget(
            f"{args.catalyst_url.rstrip('/')}/api/v1", bearer_token=token
        ),
    }
    if args.echo_url:
        proxy_targets.update(
            {
                "/api/v1/echo/api/v1": ProxyTarget(
                    f"{args.echo_url.rstrip('/')}/api/v1", bearer_token=token
                ),
                "/api/v1/echo": ProxyTarget(
                    f"{args.echo_url.rstrip('/')}/api/v1", bearer_token=token
                ),
            }
        )
    app = create_web_host_app(
        pairing_code=pairing_code,
        pairing_code_issued_at=time.time(),
        proxy_targets=proxy_targets,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
