"""
┌──────────────────────────────────────────────────────────────────────┐
│  📄 embedding_server.py                                              │
│  Module: tooling.aether_wsl_acceptance.embedding_server              │
│  Role: Serve real multilingual E5 ONNX embeddings over a local API.   │
│                                                                      │
│  模块职责：通过本地 API 提供真实 multilingual E5 ONNX 向量                 │
└──────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL_NAME = os.environ.get("AETHER_EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
MODEL_PATH = Path(
    os.environ.get("AETHER_EMBEDDING_MODEL_PATH", "/models/model_qint8_avx512_vnni.onnx")
)
TOKENIZER_PATH = Path(os.environ.get("AETHER_EMBEDDING_TOKENIZER_PATH", "/models/tokenizer.json"))
MODEL_REVISION = os.environ.get("AETHER_EMBEDDING_REVISION", "unknown")
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_BATCH_ITEMS = 128
MAX_TOKENS = 512
THREADS = max(1, int(os.environ.get("AETHER_EMBEDDING_THREADS", "4")))


def _load_runtime() -> tuple[ort.InferenceSession, Tokenizer]:
    """Loads the pinned CPU-only ONNX model and tokenizer from read-only task state."""
    if not MODEL_PATH.is_file() or not TOKENIZER_PATH.is_file():
        raise RuntimeError("The pinned local embedding artifacts are missing.")
    options = ort.SessionOptions()
    options.intra_op_num_threads = THREADS
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(
        str(MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"]
    )
    tokenizer = Tokenizer.from_file(str(TOKENIZER_PATH))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
    return session, tokenizer


SESSION, TOKENIZER = _load_runtime()
INPUTS = {item.name: item for item in SESSION.get_inputs()}


def _embed(texts: list[str]) -> tuple[list[list[float]], int]:
    """Embeds input text without altering it; callers supply E5 query/passage prefixes."""
    if (
        not texts
        or len(texts) > MAX_BATCH_ITEMS
        or any(not isinstance(text, str) or not text for text in texts)
    ):
        raise ValueError("Embedding input must be a non-empty text batch within the local bound.")
    encodings = TOKENIZER.encode_batch(texts)
    token_ids = np.asarray([item.ids for item in encodings], dtype=np.int64)
    attention_mask = np.asarray([item.attention_mask for item in encodings], dtype=np.int64)
    model_inputs: dict[str, np.ndarray] = {}
    for name, metadata in INPUTS.items():
        if name == "input_ids":
            values = token_ids
        elif name == "attention_mask":
            values = attention_mask
        elif name == "token_type_ids":
            values = np.zeros_like(token_ids)
        else:
            raise RuntimeError("The pinned ONNX model exposes an unsupported input name.")
        if "int32" in metadata.type:
            values = values.astype(np.int32, copy=False)
        model_inputs[name] = values

    output = SESSION.run(None, model_inputs)[0]
    if output.ndim == 3:
        weights = attention_mask.astype(np.float32)[..., None]
        vectors = (output.astype(np.float32) * weights).sum(axis=1) / np.maximum(
            weights.sum(axis=1), 1.0
        )
    elif output.ndim == 2:
        vectors = output.astype(np.float32)
    else:
        raise RuntimeError("The pinned ONNX model returned an unsupported output shape.")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.isfinite(vectors).all() or np.any(norms <= 0):
        raise RuntimeError("The pinned ONNX model returned a non-finite or zero embedding.")
    vectors = vectors / norms
    token_count = sum(len(item.ids) for item in encodings)
    return vectors.astype(np.float32).tolist(), token_count


class Handler(BaseHTTPRequestHandler):
    """Implements a local, unauthenticated API restricted to the private task network."""

    server_version = "CyreneLocalEmbedding/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        """Suppresses request logging so text inputs never enter container logs."""
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Serves readiness and model discovery routes."""
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "model": MODEL_NAME,
                    "revision": MODEL_REVISION,
                    "dimensions": 384,
                },
            )
            return
        if path == "/v1/models" or path == "/models":
            self._send_json(
                200, {"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]}
            )
            return
        self._send_json(404, {"error": {"message": "not found", "type": "not_found"}})

    def do_POST(self) -> None:
        """Returns OpenAI-compatible indexed vectors for a bounded request body."""
        if urlsplit(self.path).path != "/v1/embeddings":
            self._send_json(404, {"error": {"message": "not found", "type": "not_found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(
                    413,
                    {
                        "error": {
                            "message": "request body exceeds local bound",
                            "type": "invalid_request_error",
                        }
                    },
                )
                return
            request = json.loads(self.rfile.read(length))
            raw_input = request.get("input") if isinstance(request, dict) else None
            texts = [raw_input] if isinstance(raw_input, str) else raw_input
            if not isinstance(texts, list) or any(not isinstance(item, str) for item in texts):
                raise ValueError("input must be a string or a list of strings")
            vectors, token_count = _embed(texts)
            response_model = request.get("model") or MODEL_NAME
            data = [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ]
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": data,
                    "model": response_model,
                    "usage": {"prompt_tokens": token_count, "total_tokens": token_count},
                },
            )
        except (ValueError, json.JSONDecodeError):
            self._send_json(
                400,
                {
                    "error": {
                        "message": "invalid embedding request",
                        "type": "invalid_request_error",
                    }
                },
            )
        except Exception:  # noqa: BLE001 - avoid echoing model/runtime input details
            self._send_json(
                500,
                {"error": {"message": "local embedding inference failed", "type": "server_error"}},
            )


def main() -> None:
    """Starts the local model endpoint on its private container network."""
    server = ThreadingHTTPServer(("0.0.0.0", 8081), Handler)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
