"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: Text Model Lifecycle V1 public API client                   │
│ Role: One explicit Product action per command, with returned refs.  │
│ 模块职责：通过公开接口逐步操作；本地文件仅保存选择，不拥有产品状态。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from cy_artifacts import ArtifactKind, LocalArtifactProvider
from huggingface_hub import snapshot_download

RUNTIME_PROFILE = "CYRENE_TEXT_LIFECYCLE_V1_LOCAL_GPU"


class ProductClient:
    """URLs and bearer credentials are operator configuration, never saved in bookmarks."""

    def request(
        self,
        product: str,
        method: str,
        path: str,
        *,
        body: Any = None,
        data: bytes | None = None,
        key: str | None = None,
    ) -> Any:
        variable = "CYRENE_" + product.upper() + "_URL"
        origin = os.environ.get(variable, "").rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Configure a credential-free Product URL in " + variable)
        headers = {"Accept": "application/json"}
        token = os.environ.get("CYRENE_" + product.upper() + "_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            headers["Content-Type"] = "application/octet-stream"
        if key:
            headers["Idempotency-Key"] = key
        try:
            with urlopen(
                Request(origin + path, data=data, method=method, headers=headers),
                timeout=1800,
            ) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                code = json.loads(exc.read(65536)).get("code", "PRODUCT_REQUEST_REJECTED")
            except (ValueError, AttributeError):
                code = "PRODUCT_REQUEST_REJECTED"
            raise ValueError(f"{product}: HTTP {exc.code}, {code}") from None
        except URLError:
            raise ValueError(product + ": Product connection failed") from None


def select(items: list[Any], index: int, label: str) -> Any:
    if index < 1 or index > len(items):
        raise ValueError(f"Select {label} by its displayed index (1 through {len(items)})")
    return items[index - 1]


def require(state: dict[str, Any], name: str) -> Any:
    if name not in state:
        raise ValueError("No selected " + name + "; complete or select that Product resource first")
    return state[name]


def _runtime_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.expanduser().is_absolute():
        raise ValueError("CYRENE_RUNTIME_CONFIG must name the private runtime manifest")
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_mode & 0o077:
        raise ValueError("Runtime manifest must exist with mode 0600")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Runtime manifest is invalid")
    if value.get("profile") != RUNTIME_PROFILE or value.get("status") != "READY":
        raise ValueError("Runtime manifest is not READY for the lifecycle profile")
    home_value, artifact_value = value.get("runtimeHome"), value.get("artifactRoot")
    if not isinstance(home_value, str) or not isinstance(artifact_value, str):
        raise TypeError("Runtime manifest omits Artifact Plane configuration")
    home, artifact_root = Path(home_value), Path(artifact_value)
    if (
        not home.is_absolute()
        or not artifact_root.is_absolute()
        or not artifact_root.resolve().is_relative_to(home.resolve())
    ):
        raise ValueError("Runtime Artifact Plane must be bounded by runtime home")
    return value


def import_base_artifact(path: Path | None, repository: str, revision: str) -> dict[str, Any]:
    """Prepare an immutable base Artifact without requiring a Reactor deployment host."""

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Base model must be a Hugging Face owner/repository")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Base model revision must be an immutable 40-character commit")
    runtime = _runtime_config(path)
    runtime_home = Path(runtime["runtimeHome"]).resolve()
    artifact_root = Path(runtime["artifactRoot"]).resolve()
    cache = runtime_home / "cache" / "huggingface"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = Path(snapshot_download(repo_id=repository, revision=revision, cache_dir=cache))
    if not snapshot.resolve().is_relative_to(cache.resolve()) or not snapshot.is_dir():
        raise ValueError("Downloaded model snapshot escaped the runtime cache")
    provider = LocalArtifactProvider(artifact_root)
    file_count = 0
    with tempfile.TemporaryDirectory(prefix="base-import-", dir=artifact_root) as directory:
        copied = Path(directory)
        for member in snapshot.rglob("*"):
            if member.is_dir():
                continue
            resolved = member.resolve(strict=True)
            if not resolved.is_relative_to(cache.resolve()) or not resolved.is_file():
                raise ValueError("Downloaded model member escaped the runtime cache")
            relative = PurePosixPath(member.relative_to(snapshot).as_posix())
            target = copied / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved, target)
            file_count += 1
        if file_count == 0:
            raise ValueError("Downloaded model snapshot is empty")
        reference = provider.publish_portable_directory(copied, kind=ArtifactKind.MODEL)
    return {
        "modelArtifact": reference.to_dict(),
        "modelInfo": {
            "format": "huggingface.snapshot.v1",
            "fileCount": file_count,
            "source": {"repository": repository, "revision": revision},
        },
    }


def perform(args: argparse.Namespace, state: dict[str, Any], client: ProductClient) -> Any:
    """Each branch performs only the named user action through documented APIs."""
    action = args.action
    api = client.request
    if action == "dataset-import":
        dataset = api(
            "catalyst",
            "POST",
            "/api/v1/datasets",
            body={"name": args.name},
            key=args.name,
        )
        state["dataset"] = dataset
        query = urlencode({"name": args.name, "filename": args.file.name})
        state["preparation"] = api(
            "catalyst",
            "POST",
            f"/api/v1/datasets/{dataset['id']}/preparations?{query}",
            data=args.file.read_bytes(),
            key="import:" + args.name,
        )
        return state["preparation"]
    if action.startswith("dataset-"):
        prep = require(state, "preparation")
        path = "/api/v1/preparations/" + prep["id"]
        if action == "dataset-map":
            state["preparation"] = api(
                "catalyst",
                "PATCH",
                path + "/mapping",
                body={
                    "mapping": {
                        "mode": "instruction",
                        "instruction": {"field": args.instruction_field},
                        "input": {"field": args.input_field},
                        "output": {"field": args.output_field},
                    },
                    "normalization": {},
                },
            )
        elif action == "dataset-split":
            state["preparation"] = api(
                "catalyst",
                "PATCH",
                path + "/split",
                body={"split": {"trainRatio": args.train_ratio}},
            )
        elif action == "dataset-confirm":
            state["preparation"] = api("catalyst", "POST", path + "/confirm")
        elif action == "dataset-publish":
            published = api("catalyst", "POST", path + "/publish", key="publish:" + prep["id"])
            state["preparation"], state["datasetVersion"] = (
                published["preparation"],
                published["datasetVersion"],
            )
            return published["datasetVersion"]
        return state["preparation"]
    if action == "send-to-yield":
        version = require(state, "datasetVersion")
        receipt = api(
            "catalyst",
            "POST",
            f"/api/v1/dataset-versions/{version['id']}/actions/send-to-yield",
        )
        state["trainingDraft"] = api(
            "yield", "GET", "/api/v1/training-drafts/" + receipt["targetResource"]["id"]
        )
        return receipt
    if action == "serving-bindings":
        bindings = api("reactor", "GET", "/api/v1/serving-bindings")
        return bindings
    if action == "base-import":
        state["baseImport"] = import_base_artifact(
            args.runtime_config,
            args.repository,
            args.revision,
        )
        return state["baseImport"]
    if action == "training-configure":
        draft, imported = require(state, "trainingDraft"), require(state, "baseImport")
        parameters = {
            "epochs": args.epochs,
            "maxSequenceLength": args.max_length,
            "template": args.template,
            "loraRank": args.rank,
            "loraAlpha": args.alpha,
        }
        if args.max_steps is not None:
            parameters["maxSteps"] = args.max_steps
        state["trainingDraft"] = api(
            "yield",
            "PATCH",
            "/api/v1/training-drafts/" + draft["id"],
            body={
                "baseModel": {
                    "artifact": imported["modelArtifact"],
                    "source": imported["modelInfo"]["source"],
                },
                "parameters": parameters,
            },
        )
        return state["trainingDraft"]
    if action == "training-start":
        draft = require(state, "trainingDraft")
        state["trainingRun"] = api(
            "yield", "POST", f"/api/v1/training-drafts/{draft['id']}/actions/start"
        )
        return state["trainingRun"]
    if action in {"training-result", "training-cancel"}:
        run = require(state, "trainingRun")
        path = "/api/v1/training-runs/" + run["id"]
        state["trainingRun"] = api(
            "yield",
            "POST" if action == "training-cancel" else "GET",
            path + ("/actions/cancel" if action == "training-cancel" else ""),
        )
        if state["trainingRun"].get("result") is not None:
            state["trainingResult"] = state["trainingRun"]["result"]
        return state["trainingRun"]
    if action == "send-to-reactor":
        result = require(state, "trainingResult")
        receipt = api(
            "yield",
            "POST",
            f"/api/v1/training-results/{result['id']}/actions/send-to-reactor",
        )
        state["deploymentDraft"] = api(
            "reactor",
            "GET",
            "/api/v1/deployment-drafts/" + receipt["targetResource"]["id"],
        )
        return receipt
    if action == "deploy":
        draft = require(state, "deploymentDraft")
        bindings = api("reactor", "GET", "/api/v1/serving-bindings")
        binding = select(bindings, args.binding_index, "serving binding")["bindingId"]
        state["servingBinding"] = binding
        node = api("reactor", "GET", f"/api/v1/serving-bindings/{quote(binding, safe='')}/node")
        state["deployment"] = api(
            "reactor",
            "POST",
            f"/api/v1/deployment-drafts/{draft['id']}/actions/deploy",
            body={
                "name": args.name,
                "servingBindingId": binding,
                "nodeRef": node["nodeRef"],
            },
        )
        return state["deployment"]
    if action in {"deployment-status", "deployment-stop"}:
        deployment = require(state, "deployment")
        path = "/api/v1/deployments/" + deployment["id"]
        state["deployment"] = api(
            "reactor",
            "POST" if action == "deployment-stop" else "GET",
            path + ("/actions/stop" if action == "deployment-stop" else ""),
        )
        return state["deployment"]
    if action == "exchange-endpoint-create":
        state["gatewayEndpoint"] = api(
            "exchange",
            "POST",
            "/api/v1/gateway-endpoints",
            body={
                "name": args.name,
                "publicBaseUrl": args.public_url,
                "authPolicyRef": args.auth_policy_ref,
            },
            key="lifecycle-endpoint:" + args.name,
        )
        return state["gatewayEndpoint"]
    if action in {"exchange-receivers", "send-to-exchange"}:
        receivers = api("reactor", "GET", "/api/v1/exchange-receivers")
        if action == "exchange-receivers":
            return receivers
        receiver = select(receivers, args.receiver_index, "Exchange receiver")
        target = select(receiver["providerBindingIds"], args.provider_index, "provider binding")
        deployment = require(state, "deployment")
        endpoint = api("reactor", "GET", "/api/v1/endpoints/" + deployment["endpointId"])
        receipt = api(
            "reactor",
            "POST",
            f"/api/v1/endpoints/{endpoint['id']}/actions/send-to-exchange",
            body={
                "resourceVersion": endpoint["resourceVersion"],
                "receiverId": receiver["receiverId"],
                "gatewayEndpointId": require(state, "gatewayEndpoint")["id"],
                "targetBindingId": target,
                "modelPattern": args.model,
            },
            key="lifecycle-route:" + endpoint["id"] + ":" + args.model,
        )
        state["route"] = receipt["route"]
        return receipt
    if action == "route-enable":
        route = require(state, "route")
        current = api("exchange", "GET", "/api/v1/gateway-routes/" + route["id"])
        state["route"] = api(
            "exchange",
            "POST",
            f"/api/v1/gateway-route-drafts/{route['id']}/actions/confirm",
            body={"resourceVersion": current["resourceVersion"]},
        )
        return state["route"]
    if action in {"navigator-sessions", "send-to-echo"}:
        prefix = "/api/v1/harness/workspaces/" + quote(args.workspace, safe="") + "/sessions"
        sessions = api("navigator", "GET", prefix)["items"]
        if action == "navigator-sessions":
            return sessions
        session = select(sessions, args.session_index, "Navigator session")
        session_id = session["productMetadata"]["sessionId"]
        source = prefix + "/" + quote(session_id, safe="")
        snapshot = api("navigator", "GET", source)
        lineage = []
        if "trainingResult" in state:
            result = state["trainingResult"]
            lineage.extend(
                [
                    result["modelVersion"]["id"],
                    result["datasetVersion"]["uri"],
                    result["trainingRun"]["uri"],
                ]
            )
        if "route" in state:
            lineage.append("cyrene://exchange/gateway-routes/" + state["route"]["id"])
        body = {"expectedRevision": snapshot["revision"], "provenanceRefs": lineage}
        receipt = api("navigator", "POST", source + "/actions/send-to-echo", body=body)
        state["evaluationInput"] = api(
            "echo",
            "GET",
            "/api/v1/evaluation-inputs/" + receipt["targetResource"]["id"],
        )
        return receipt
    if action == "evaluation-input":
        resource = require(state, "evaluationInput")
        return api("echo", "GET", f"/api/v1/evaluation-inputs/{resource['id']}/samples")
    if action == "evaluate":
        resource = require(state, "evaluationInput")
        suite = api(
            "echo",
            "POST",
            "/api/v1/evaluation-suites",
            body={
                "name": "Explicit reference review",
                "evaluator": "exact_match.v1",
                "expectedField": "expected",
                "actualField": "output",
                "threshold": 1,
            },
            key="reference-suite:v1",
        )
        state["evaluationRun"] = api(
            "echo",
            "POST",
            f"/api/v1/evaluation-inputs/{resource['id']}/actions/evaluate",
            body={
                "suiteId": suite["id"],
                "engineBindingId": "local-exact-match",
                "referenceAnswers": {
                    str(index + 1): value for index, value in enumerate(args.reference_answer)
                },
            },
        )
        return state["evaluationRun"]
    if action == "annotate":
        run = require(state, "evaluationRun")
        annotation = api(
            "echo",
            "POST",
            f"/api/v1/evaluation-runs/{run['id']}/annotations",
            body={
                "sampleIndex": args.sample,
                "reviewer": args.reviewer,
                "manualScore": args.score,
                "correctedOutput": args.correction,
            },
        )
        state.setdefault("annotations", {})[str(args.sample)] = annotation
        return annotation
    if action == "feedback-create":
        run = require(state, "evaluationRun")
        annotations = state.get("annotations", {})
        state["feedbackSet"] = api(
            "echo",
            "POST",
            "/api/v1/feedback-sets",
            body={
                "name": args.name,
                "runId": run["id"],
                "sampleIndexes": args.sample,
                "annotationIds": [
                    annotations[str(index)]["id"]
                    for index in args.sample
                    if str(index) in annotations
                ],
            },
            key="selected-feedback:" + run["id"] + ":" + args.name,
        )
        return state["feedbackSet"]
    if action == "send-to-catalyst":
        feedback = require(state, "feedbackSet")
        body = (
            {}
            if args.new_dataset or "dataset" not in state
            else {"datasetId": state["dataset"]["id"]}
        )
        receipt = api(
            "echo",
            "POST",
            f"/api/v1/feedback-sets/{feedback['id']}/actions/send-to-catalyst",
            body=body,
        )
        state["preparation"] = api(
            "catalyst", "GET", "/api/v1/preparations/" + receipt["targetResource"]["id"]
        )
        state["dataset"] = api(
            "catalyst", "GET", "/api/v1/datasets/" + state["preparation"]["datasetId"]
        )
        return receipt
    raise ValueError("Unknown action")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="One explicit Cyrene Product action per command; no automatic pipeline"
    )
    result.add_argument("--selection", type=Path, default=Path("text-lifecycle-selection.json"))
    commands = result.add_subparsers(dest="action", required=True)
    command = commands.add_parser("dataset-import")
    command.add_argument("file", type=Path)
    command.add_argument("--name", required=True)
    command = commands.add_parser("dataset-map")
    command.add_argument("--instruction-field", default="instruction")
    command.add_argument("--input-field", default="input")
    command.add_argument("--output-field", default="output")
    command = commands.add_parser("dataset-split")
    command.add_argument("--train-ratio", type=float, default=1)
    for action in (
        "dataset-confirm",
        "dataset-publish",
        "send-to-yield",
        "serving-bindings",
        "training-start",
        "training-result",
        "training-cancel",
        "send-to-reactor",
        "deployment-status",
        "deployment-stop",
        "exchange-receivers",
        "route-enable",
        "evaluation-input",
    ):
        commands.add_parser(action)
    command = commands.add_parser("base-import")
    command.add_argument("--repository", required=True)
    command.add_argument("--revision", required=True)
    command.add_argument(
        "--runtime-config",
        type=Path,
        default=Path(os.environ["CYRENE_RUNTIME_CONFIG"])
        if os.environ.get("CYRENE_RUNTIME_CONFIG")
        else None,
    )
    command = commands.add_parser("training-configure")
    command.add_argument("--epochs", type=float, default=1)
    command.add_argument("--max-steps", type=int)
    command.add_argument("--max-length", type=int, default=512)
    command.add_argument("--template", default="default")
    command.add_argument("--rank", type=int, default=8)
    command.add_argument("--alpha", type=int, default=16)
    command = commands.add_parser("deploy")
    command.add_argument("--name", default="Text lifecycle v1")
    command.add_argument("--binding-index", type=int, default=1)
    command = commands.add_parser("exchange-endpoint-create")
    command.add_argument("--name", required=True)
    command.add_argument("--public-url", required=True)
    command.add_argument("--auth-policy-ref", required=True)
    command = commands.add_parser("send-to-exchange")
    command.add_argument("--receiver-index", type=int, default=1)
    command.add_argument("--provider-index", type=int, default=1)
    command.add_argument("--model", required=True)
    for action in ("navigator-sessions", "send-to-echo"):
        command = commands.add_parser(action)
        command.add_argument("--workspace", required=True)
        command.add_argument("--session-index", type=int, default=1)
    command = commands.add_parser("evaluate")
    command.add_argument(
        "--reference-answer",
        action="append",
        required=True,
        help="One user reference answer per imported output, in displayed order",
    )
    command = commands.add_parser("annotate")
    command.add_argument("--sample", type=int, required=True)
    command.add_argument("--correction", required=True)
    command.add_argument("--score", type=float, required=True)
    command.add_argument("--reviewer", required=True)
    command = commands.add_parser("feedback-create")
    command.add_argument("--sample", type=int, action="append", required=True)
    command.add_argument("--name", required=True)
    command = commands.add_parser("send-to-catalyst")
    command.add_argument("--new-dataset", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        state = json.loads(args.selection.read_text()) if args.selection.exists() else {}
        result = perform(args, state, ProductClient())
        # This file contains returned resources, never environment values or credentials.
        pending = args.selection.with_suffix(".pending")
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(args.selection)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, TypeError, OSError, KeyError) as exc:
        print("Action did not complete: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
