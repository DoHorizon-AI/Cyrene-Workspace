"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_release_deploy.py                                          │
│  Module: tooling.deploy.teams_cloud.tests.test_release_deploy        │
│  Role: Exercise release sequencing and uncertain-outcome recovery.  │
│                                                                     │
│  模块职责：用明确的本地模拟验证发布顺序、失败边界和禁止重复执行。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
deployment = importlib.import_module("release_deploy")
release_bundle = importlib.import_module("release_bundle")


class SimulatedCanonicalBackend:
    """Stateful test adapter; none of its receipts are external acceptance evidence."""

    def __init__(self, policy: Any, active: dict[str, Any]) -> None:
        self.policy = policy
        self.active = copy.deepcopy(active)
        self.calls: list[tuple[str, str]] = []
        self.outcomes: dict[str, str] = {}
        self.fail_target: str | None = None
        self.fail_state = "RECOVERY_REQUIRED"
        self.post_probe_failure = False
        self.live_override: dict[str, str] = {}
        self.drift_after: str | None = None

    def selection(self) -> dict[str, Any]:
        """Read the simulated selector, so external test changes are observable."""

        return deployment._json(self.policy.selection)

    def image_rows(self, selected: dict[str, Any]) -> dict[str, Any]:
        """Return only the test adapter's actual accepted lock rows."""

        return deployment.runtime_update._image_rows_by_service(self.active)

    def live_image(self, target: str) -> str:
        """Allow a test to distinguish desired lock state from actual runtime state."""

        return self.live_override.get(
            target,
            self.image_rows({})[deployment.runtime_update.LOCK_SERVICE_FOR_TARGET[target]][
                "imageId"
            ],
        )

    def ensure_image(
        self, step: Path, asset: Path, image: dict[str, Any], locked: dict[str, Any]
    ) -> None:
        """Record a simulated import without invoking Docker."""

        self.calls.append(("import", step.name))

    def wait_window(self, target: str) -> None:
        """Record the window guard before any native update plan is made."""

        self.calls.append(("window", target))

    def probe(self, run_id: str, workspace: str) -> dict[str, Any]:
        """Return an explicitly simulated native probe contract."""

        self.calls.append(("probe", run_id))
        if (
            self.post_probe_failure
            and all(value == "APPLIED" for value in self.outcomes.values())
            and self.outcomes
        ):
            return {"status": "SIMULATED_PROVIDER_FAILURE"}
        task_id = "unit-test-only-" + run_id
        evidence = self.policy.root / ("simulated-evidence-" + run_id + ".json")
        deployment._write(
            evidence,
            {
                "schemaVersion": "cyrene-connector-auth-readonly-evidence-v1",
                "executionPath": "exchange_connector_api",
                "taskId": task_id,
            },
        )
        return {
            "status": "REAL_NATIVE_EXCHANGE_READONLY_PROBE_PASS",
            "taskId": task_id,
            "evidencePath": str(evidence),
            "evidenceSha256": deployment._sha(evidence),
        }

    def plan(
        self,
        step: Path,
        target: str,
        image: dict[str, Any],
        published: dict[str, Any],
        probe: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a fake operation descriptor; canonical plan tests live separately."""

        self.calls.append(("plan", target))
        plan = {
            "operationId": f"00000000-0000-4000-8000-{len(self.outcomes) + 1:012d}",
            "planSha256": hashlib.sha256(target.encode()).hexdigest(),
            "image": image,
            "target": target,
        }
        deployment._write(step / "plan.json", plan, exclusive=True)
        return plan

    def github_acceptance(self, run_id: str) -> dict[str, Any]:
        """Model a separately verified GitHub API read acceptance receipt."""

        evidence = self.policy.root / ("simulated-github-" + run_id + ".json")
        result = {
            "status": "REAL_NATIVE_GITHUB_READONLY_PROBE_PASS",
            "taskId": run_id,
            "activeOperationId": self.selection()["operationId"],
        }
        deployment._write(
            evidence, {"schemaVersion": "cyrene-github-readonly-acceptance-evidence-v1", **result}
        )
        return {
            **result,
            "evidencePath": str(evidence),
            "evidenceSha256": deployment._sha(evidence),
        }

    def apply(self, step: Path, plan: dict[str, Any]) -> dict[str, Any]:
        """Model the single selected image being replaced or an unknown operation."""

        target = plan["target"]
        self.calls.append(("apply", target))
        if target == self.fail_target:
            self.outcomes[target] = self.fail_state
            raise deployment.DeploymentError("SIMULATED_UPDATE_FAILURE", state=self.fail_state)
        service = deployment.runtime_update.LOCK_SERVICE_FOR_TARGET[target]
        for row in self.active["images"]:
            if row["service"] == service:
                row.update(
                    {key: plan["image"][key] for key in ("image", "imageId", "sourceCommits")}
                )
        self.outcomes[target] = "APPLIED"
        selected = self.selection()
        selected.update(operationId=plan["operationId"], planSha256=plan["planSha256"])
        selected["imageLock"]["sha256"] = hashlib.sha256(
            json.dumps(self.active).encode()
        ).hexdigest()
        if target == self.drift_after:
            selected["operationId"] = "another-writer-operation"
        deployment._write(self.policy.selection, selected)
        return {"state": "APPLIED"}

    def status(self, step: Path) -> dict[str, Any] | None:
        """Expose canonical-looking state only for operations actually prepared."""

        if not (step / "plan.json").exists():
            return None
        plan = deployment._json(step / "plan.json")
        return {
            "operationId": plan["operationId"],
            "planSha256": plan["planSha256"],
            "state": self.outcomes.get(step.name, "PREPARED_NO_APPLY_JOURNAL"),
        }


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build local simulated release bytes, preserving real manifest validation."""

    manifest = json.loads(
        (Path(__file__).parent / "fixtures/simulated-release-manifest-v1.json").read_text()
    )
    root = tmp_path / ".artifacts/releases"
    root.mkdir(parents=True)
    selection = tmp_path / "selector.json"
    deployment._write(
        selection,
        {"operationId": "initial", "planSha256": "1" * 64, "imageLock": {"sha256": "2" * 64}},
    )
    policy = deployment.DeploymentPolicy(
        path=tmp_path / "policy.json",
        sha256="3" * 64,
        selection=selection,
        journals=tmp_path / "journals",
        root=root,
        probe_policy=tmp_path / "probe.json",
        probe_policy_sha256="4" * 64,
        targets=("github-issues", "exchange"),
        max_memory_mib=3584,
        max_cpus=1.95,
        required_free_gib=0.001,
        minimum_available_mib=1024,
        drain_seconds=600,
        health_seconds=120,
        window_wait_seconds=0,
        ingress_exposure="host-fronted-staging",
    )
    deployment._write(policy.probe_policy, {"runtime": {"outputDirectory": str(root)}})
    published = {
        "schemaVersion": "cyrene-teams-cloud-image-lock-v1",
        "targetPlatform": "linux/arm64",
        "state": "LOCAL_ONLY",
        "sources": {},
        "externalImages": {},
        "baseImages": {},
        "images": [],
    }
    for alias, source in manifest["sources"].items():
        published["sources"][alias] = {
            "commit": source["commit"],
            "remote": "https://github.com/" + source["repository"] + ".git",
            "tree": "a" * 40,
        }
    for image in manifest["images"]:
        image["archive"]["sizeBytes"] = len(image["service"].encode())
        published["images"].append(
            {
                key: image[key]
                for key in ("service", "image", "imageId", "platform", "sourceCommits")
            }
        )
        published["images"][-1].update(sizeBytes=1000, registryDigest=None)
    raw_lock = json.dumps(published).encode()
    manifest["imageLock"].update(
        sizeBytes=len(raw_lock), sha256=hashlib.sha256(raw_lock).hexdigest()
    )
    source = tmp_path / "release-manifest.json"
    source.write_text(json.dumps(manifest, indent=2) + "\n")
    release_bundle.validate_release_manifest(manifest)
    active = copy.deepcopy(published)
    for index, row in enumerate(active["images"], start=20):
        row.update(image=f"cyrene/{row['service']}:previous", imageId="sha256:" + f"{index:064x}")
    adapter = SimulatedCanonicalBackend(policy, active)

    def local_directory(path: Path, *, create: bool) -> None:
        # Root ownership itself is tested independently; ordinary CI runs as non-root.
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert path.is_dir() and not path.is_symlink()

    def local_download(directory: Path, url: str, record: dict[str, Any]) -> Path:
        destination = deployment._asset_path(directory, record)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            raw_lock
            if record["file"] == "image-lock.json"
            else Path(record["file"]).name.removesuffix(".docker.tar.gz").encode()
        )
        assert deployment._sha(destination) == record["sha256"]
        return destination

    monkeypatch.setattr(deployment, "_directory", local_directory)
    monkeypatch.setattr(deployment, "download_asset", local_download)
    return {
        "policy": policy,
        "manifest": manifest,
        "manifestPath": source,
        "manifestSha256": deployment._sha(source),
        "backend": adapter,
        "published": published,
        "url": f"https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/{manifest['releaseId']}",
    }


def _deploy(value: dict[str, Any], *, execute: bool = True) -> dict[str, Any]:
    """Run the real release orchestrator against the explicit local simulation."""

    return deployment.deploy_release(
        value["policy"],
        value["manifestPath"],
        value["manifestSha256"],
        value["url"],
        execute=execute,
        backend=value["backend"],
    )


def test_release_uses_serial_native_operations_and_post_acceptance(
    environment: dict[str, Any],
) -> None:
    result = _deploy(environment)
    assert result["state"] == "APPLIED"
    assert [value for kind, value in environment["backend"].calls if kind == "apply"] == [
        "github-issues",
        "exchange",
    ]
    assert sum(kind == "probe" for kind, _ in environment["backend"].calls) == 3
    assert len(result["steps"]) == 2
    assert all(value["state"] == "APPLIED" for value in result["steps"])
    assert not list(environment["policy"].root.glob("*/images/*.docker.tar.gz"))


def test_dry_run_never_imports_probes_or_starts_an_update(environment: dict[str, Any]) -> None:
    result = _deploy(environment, execute=False)
    assert result["state"] == "PLANNED"
    assert result["targets"] == ["github-issues", "exchange"]
    assert environment["backend"].calls == []
    assert not list(environment["policy"].root.glob("*/deployment-binding.json"))


def test_duplicate_success_delivery_only_reads_existing_journals(
    environment: dict[str, Any],
) -> None:
    _deploy(environment)
    before = list(environment["backend"].calls)
    assert _deploy(environment)["state"] == "APPLIED"
    assert environment["backend"].calls == before


@pytest.mark.parametrize("failure", ["RECOVERY_REQUIRED", "APPLYING", "FAILED_ROLLED_BACK"])
def test_partial_failure_keeps_prior_success_and_never_replays(
    environment: dict[str, Any], failure: str
) -> None:
    adapter = environment["backend"]
    adapter.fail_target = "exchange"
    adapter.fail_state = failure
    with pytest.raises(deployment.DeploymentError, match="SIMULATED_UPDATE_FAILURE"):
        _deploy(environment)
    assert adapter.outcomes["github-issues"] == "APPLIED"
    assert adapter.outcomes["exchange"] == failure
    before = list(adapter.calls)
    result = _deploy(environment)
    assert result["state"] == ("FAILED" if failure == "FAILED_ROLLED_BACK" else "RECOVERY_REQUIRED")
    assert adapter.calls == before


def test_live_runtime_must_match_unchanged_lock(environment: dict[str, Any]) -> None:
    adapter = environment["backend"]
    adapter.active = copy.deepcopy(environment["published"])
    adapter.live_override["exchange"] = "sha256:" + "a" * 64
    with pytest.raises(
        deployment.DeploymentError, match="UNCHANGED_LOCK_DIFFERS_FROM_LIVE_RUNTIME"
    ):
        _deploy(environment)
    assert adapter.calls == []


def test_same_images_still_require_real_read_acceptance(environment: dict[str, Any]) -> None:
    adapter = environment["backend"]
    adapter.active = copy.deepcopy(environment["published"])
    assert _deploy(environment)["state"] == "NO_CHANGE"
    assert len(adapter.calls) == 1 and adapter.calls[0][0] == "probe"


def test_failed_post_cutover_probe_is_never_reported_as_applied(
    environment: dict[str, Any],
) -> None:
    adapter = environment["backend"]
    adapter.post_probe_failure = True
    # The second preparation probe sees the first completed target, so fail only
    # after both target updates have really completed in this local adapter.
    original = adapter.probe

    def probe(run_id: str, workspace: str) -> dict[str, Any]:
        adapter.post_probe_failure = len(adapter.outcomes) == 2
        return original(run_id, workspace)

    adapter.probe = probe
    with pytest.raises(deployment.DeploymentError, match="POST_CUTOVER_REAL_ACCEPTANCE_FAILED"):
        _deploy(environment)
    adapter.post_probe_failure = False
    adapter.probe = original
    before = [value for kind, value in adapter.calls if kind == "apply"]
    result = _deploy(environment)
    assert result["state"] == "NO_CHANGE"
    assert [value for kind, value in adapter.calls if kind == "apply"] == before
    assert all(value == "APPLIED" for value in adapter.outcomes.values())


def test_other_writer_selector_change_stops_before_next_target(environment: dict[str, Any]) -> None:
    adapter = environment["backend"]
    adapter.drift_after = "github-issues"
    with pytest.raises(
        deployment.DeploymentError, match="CANONICAL_SELECTOR_OR_ACTUAL_IMAGE_MISMATCH"
    ):
        _deploy(environment)
    assert [value for kind, value in adapter.calls if kind == "apply"] == ["github-issues"]


def test_wrong_manifest_digest_rejects_before_any_import(environment: dict[str, Any]) -> None:
    environment["manifestSha256"] = "0" * 64
    with pytest.raises(deployment.DeploymentError, match="RELEASE_MANIFEST_PIN_MISMATCH"):
        _deploy(environment)
    assert environment["backend"].calls == []


def test_lock_slice_preserves_every_unselected_image_and_external_lock(
    environment: dict[str, Any],
) -> None:
    active = environment["backend"].active
    result = deployment.slice_image_lock(active, environment["published"], "exchange")
    assert result["externalImages"] == active["externalImages"]
    assert [row for row in result["images"] if row["service"] != "exchange"] == [
        row for row in active["images"] if row["service"] != "exchange"
    ]
    assert result["images"] != active["images"]


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/releases/download/x",
        "http://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/x",
        "https://user:password@github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/x",
    ],
)
def test_release_url_cannot_redirect_bootstrap_to_other_authority(value: str) -> None:
    with pytest.raises(deployment.DeploymentError, match="RELEASE_DOWNLOAD_ORIGIN_INVALID"):
        deployment.validate_release_url(value, "teams-arm64-aaaaaaaaaaaa-bbbbbbbbbbbb")


def _image_tar(path: Path, *, extra_tag: bool = False, link: bool = False) -> dict[str, Any]:
    """Write a tiny Docker-save format archive for parser tests only."""

    config = json.dumps(
        {
            "architecture": "arm64",
            "os": "linux",
            "config": {"Labels": {"io.cyrene.source.exchange.revision": "a" * 40}},
        }
    ).encode()
    digest = hashlib.sha256(config).hexdigest()
    tag = "cyrene/exchange:fixture"
    manifest = [
        {
            "Config": digest + ".json",
            "RepoTags": [tag, "cyrene/other:bad"] if extra_tag else [tag],
            "Layers": ["layer/layer.tar"],
        }
    ]
    with tarfile.open(path, "w:gz") as archive:
        for name, raw in [
            ("manifest.json", json.dumps(manifest).encode()),
            (digest + ".json", config),
            ("layer/layer.tar", b"unit layer"),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        if link:
            member = tarfile.TarInfo("outside")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
    return {"imageId": "sha256:" + digest, "image": tag, "sourceCommits": {"exchange": "a" * 40}}


def test_archive_stream_parser_accepts_exact_image_and_rejects_retagging(tmp_path: Path) -> None:
    path = tmp_path / "image.tar.gz"
    image = _image_tar(path)
    deployment.verify_image_archive(path, image, 10000)
    _image_tar(path, extra_tag=True)
    with pytest.raises(deployment.DeploymentError, match="SOLE_LOCKED_IMAGE"):
        deployment.verify_image_archive(path, image, 10000)


def test_archive_stream_parser_rejects_links_and_expansion_limit(tmp_path: Path) -> None:
    path = tmp_path / "image.tar.gz"
    image = _image_tar(path, link=True)
    with pytest.raises(deployment.DeploymentError, match="LINK_OR_SPECIAL_FILE"):
        deployment.verify_image_archive(path, image, 10000)
    _image_tar(path)
    with pytest.raises(deployment.DeploymentError, match="SIZE_OR_MEMBER_LIMIT"):
        deployment.verify_image_archive(path, image, 1)


def test_uncertain_image_import_is_not_repeated(
    environment: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "image.tar.gz"
    image = _image_tar(path)
    step = tmp_path / "step"
    step.mkdir()
    calls = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "info":
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(str(tmp_path)), stderr="")
        if argv[2] == "inspect":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not present")
        raise subprocess.TimeoutExpired(argv, 600)

    monkeypatch.setattr(deployment.subprocess, "run", run)
    backend = deployment.CanonicalBackend(environment["policy"])
    with pytest.raises(deployment.DeploymentError, match="IMAGE_IMPORT_TIMEOUT_NO_REPLAY"):
        backend.ensure_image(step, path, image, {"sizeBytes": 1000})
    with pytest.raises(deployment.DeploymentError, match="IMAGE_IMPORT_UNRESOLVED_NO_REPLAY"):
        backend.ensure_image(step, path, image, {"sizeBytes": 1000})
    assert sum(argv[2] == "load" for argv in calls) == 1


def test_observer_pending_resumes_without_reapplying_or_overwriting_acceptance(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost observer is independent of already accepted application updates."""

    from dataclasses import replace

    policy = replace(environment["policy"], observation_profile={"testOnly": True})
    environment["policy"] = policy
    adapter = environment["backend"]
    adapter.policy = policy
    monkeypatch.setattr(adapter, "close_observation", lambda root: None, raising=False)
    monkeypatch.setattr(
        adapter, "observation_status", lambda: {"status": "NOT_RUNNING"}, raising=False
    )
    assert _deploy(environment)["state"] == "ACCEPTANCE_PENDING"
    before = list(adapter.calls)
    receipts = list(policy.root.glob("*/post-release-acceptance-*.json"))
    assert len(receipts) == 1
    evidence_before = receipts[0].read_bytes()
    assert _deploy(environment)["state"] == "ACCEPTANCE_PENDING"
    assert adapter.calls == before
    assert receipts[0].read_bytes() == evidence_before
    monkeypatch.setattr(adapter, "observation_status", lambda: {"status": "RUNNING"})
    assert _deploy(environment)["state"] == "APPLIED"
    assert adapter.calls == before
    monkeypatch.setattr(
        adapter, "observation_status", lambda: {"status": "COMPLETED", "complete24h": True}
    )
    assert _deploy(environment)["state"] == "APPLIED"
    assert adapter.calls == before
    monkeypatch.setattr(
        adapter, "observation_status", lambda: {"status": "COMPLETED", "complete24h": False}
    )
    assert _deploy(environment)["state"] == "ACCEPTANCE_PENDING"
    assert adapter.calls == before


def test_changed_acceptance_bytes_never_remain_applied(environment: dict[str, Any]) -> None:
    """Current image IDs cannot hide modified post-deployment evidence."""

    _deploy(environment)
    root = environment["policy"].root / environment["manifest"]["releaseId"]
    receipt = deployment._json(next(root.glob("post-release-acceptance-*.json")))
    Path(receipt["evidencePath"]).write_text("{}\n")
    with pytest.raises(deployment.DeploymentError, match="ACCEPTANCE_EVIDENCE_DIGEST_CHANGED"):
        _deploy(environment)


def test_safe_preparation_retry_preserves_already_applied_target(
    environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only window failure resumes at the unstarted second operation."""

    adapter = environment["backend"]
    original = adapter.wait_window
    failed = False

    def wait(target: str) -> None:
        nonlocal failed
        if target == "exchange" and not failed:
            failed = True
            raise deployment.DeploymentError("SIMULATED_READONLY_WINDOW_UNAVAILABLE")
        original(target)

    monkeypatch.setattr(adapter, "wait_window", wait)
    with pytest.raises(deployment.DeploymentError, match="READONLY_WINDOW_UNAVAILABLE"):
        _deploy(environment)
    status = deployment.deployment_status(
        environment["policy"], environment["manifest"]["releaseId"], backend=adapter
    )
    assert status["state"] == "SAFE_TO_RESUME"
    assert status["mayResumeSafely"] is True
    assert _deploy(environment)["state"] == "APPLIED"
    assert [target for kind, target in adapter.calls if kind == "apply"] == [
        "github-issues",
        "exchange",
    ]
