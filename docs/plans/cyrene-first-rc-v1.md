# Cyrene First Usable RC V1 / 首个可用 RC

> **中文主计划 / Primary plan:** 把 Cyrene 从基础设施基线推进为个人开发者与研究者可用的首个 RC：安装、导入模型、清洗数据、单卡 SFT + LoRA、vLLM 部署、Exchange 网关与 API Key、真实客户端调用。
>
> **English summary:** First usable RC for single-user Linux NVIDIA hosts: browser console, model import, dataset curation, single-GPU SFT+LoRA, vLLM serving, Exchange gateway with API keys, and external OpenAI-compatible clients.

## 0. Document control / 文档控制

| Field | Value |
| --- | --- |
| Plan ID | CYRENE-FIRST-RC-V1 |
| Current status | IMPLEMENTING — Wave 0 baseline and Wave 2 import/gateway API foundations landed with local evidence; real GPU, HTTPS, installer and WebUI gates remain pending |
| Supported environment | Ubuntu 24.04 x86_64, one NVIDIA GPU (>= 12 GiB VRAM), browser local or remote |
| Acceptance model | `Qwen/Qwen2.5-1.5B-Instruct@5fee7c4ed634dc66c6e318c8ac2897b8b9154536` (Apache-2.0) |
| Release lock | [`release-lock.json`](../../release-lock.json) |
| Evidence record | [2026-09-20 Wave 0 / Wave 2 evidence](evidence/2026-09-20-rc-wave0-wave2.md) |
| RC exclusions | WinUI feature parity, team RBAC, multi-node scheduling, Echo feedback retraining, QLoRA, full-parameter training, multimodal |

Real acceptance (ACTUAL_PASS) requires the declared real environment: a native
Linux NVIDIA host, real model files, a real vLLM process, and a real second
device. Unit tests, fake executors, and HTTP 200 responses never produce
ACTUAL_PASS.

## 1. Status convention / 状态约定

Per the workspace status rule, every check starts `[ ]`. This document records
`CODE_COMPLETE` and `LOCAL_TEST_PASS` deltas without ticking the checkbox, and
only an Actual PASS with full evidence ticks `[x]`.

## 2. Wave 0 — Delivery baseline

- [ ] **RC-W0-01 — Web console package restored to Plugins.**
  `Cyrene-Plugins-Official/plugins/ui/navigator` now exists with README and
  `PROVENANCE.md` recording that the public history only contains a vanilla-TS
  probe shell and that the RC rebuild is React + TypeScript + Vite. Full console
  implementation remains Wave 1/2 work.
- [ ] **RC-W0-02 — Runtime capability records completed.**
  `training.llama-factory.v1` and `execution.engine.v1` both have manifests,
  owner-scoped schemas, TCK suites, provenance files, catalog entries, and
  public-CI coverage. Real CUDA/vLLM acceptance remains Wave 2/3.
- [ ] **RC-W0-03 — Developer stack drift repaired.**
  `cyrene-dev.py` no longer exports a removed Reactor worktree; the reference
  runtime no longer advertises a reactor runtime config it never writes; the
  service supervisor starts the DirectPlugin endpoint supervisor first and
  injects fresh connection references; legacy test/monkeypatch references are
  removed.
- [ ] **RC-W0-04 — Unified release lock.** [`release-lock.json`](../../release-lock.json)
  records the supported environment, the acceptance model, engine pins,
  plugin packages, and repository revisions. `scripts/validate_release_lock.py`
  validates it and names the open engine blockers.
- [ ] **RC-W0-05 — Acceptance model pinned.**
  `Qwen/Qwen2.5-1.5B-Instruct` at the exact revision is locked in the release
  lock for Wave 2/3 acceptance.
- [ ] **RC-W0-GATE — Development stack boots.** All services and required
  plugins healthy and the WebUI reachable without hand-injected connection
  references. **Pending:** requires the Wave 1 WebUI and a full `cyrene-dev up`
  run on the target host after the current repository changes are committed.

## 3. Wave 1 — Install, HTTPS, and credential foundation

- [ ] **RC-W1-01 — `.deb` and `cyrene` management command** (`init/up/down/status/doctor/logs/model import/backup/restore/upgrade/uninstall`).
- [ ] **RC-W1-02 — Installer preflight** for NVIDIA, disk, systemd, ports, CUDA compatibility.
- [ ] **RC-W1-03 — Workspace/database/artifact/credential initialization** and automatic required-plugin installation.
- [ ] **RC-W1-04 — Local loopback HTTP vs remote HTTPS mode** with Caddy or an existing reverse proxy.
- [ ] **RC-W1-05 — One-time pairing code and revocable Secure/HttpOnly/SameSite cookie sessions.**
- [ ] **RC-W1-06 — Platform-owned Credential Store** where HF tokens are write-only, encrypted at rest, resolved by `CredentialRef`, and never echoed.
- [ ] **RC-W1-GATE — Clean-host install** and remote HTTPS pairing/revocation.

## 4. Wave 2 — Model import, deployment, and gateway

- [ ] **RC-W2-01 — Reactor persisted ModelImport.**
  Implemented: `POST/GET /api/v1/model-imports`, HF repo + pinned revision,
  `CredentialRef`, absolute local path, validation evidence, `trust_remote_code`
  refusal, idempotent replay, persisted failure. Local tests pass.
- [ ] **RC-W2-02 — vLLM model import.**
  Implemented: `POST /imports` validates weights/config/tokenizer/chat template,
  resolves private credential refs from mode-0600 files, publishes a portable
  `model` artifact, and refuses remote code. Local tests and TCK pass.
- [ ] **RC-W2-03 — Real deployment path.**
  Reactor forwards artifacts to the vLLM runtime and requires identity read-back
  plus a live inference probe before READY. **Pending:** real vLLM/GPU run.
- [ ] **RC-W2-04 — Exchange resource lists.**
  Implemented: `GET /api/v1/gateway-endpoints` and `GET /api/v1/gateway-routes`.
- [ ] **RC-W2-05 — Exchange API keys.**
  Implemented: server-generated `cyk_…` secrets shown once, SHA-256 digest
  storage, name/expiry/scope, list/get/revoke, workspace isolation, idempotent
  replay without secret. CLI `key issue/list/revoke`.
- [ ] **RC-W2-06 — `/v1` gateway contract.**
  Implemented: `/v1/models` authenticates with the gateway credential and
  filters by model scope; `/v1/chat/completions` rejects out-of-scope models with
  `model_not_permitted` and audits the rejection; unary + SSE unchanged.
- [ ] **RC-W2-07 — Deployment events/observation API.** **Pending.**
- [ ] **RC-W2-08 — WebUI "deploy and publish API" wizard.** **Pending** (Wave 1 WebUI).
- [ ] **RC-W2-GATE — Real GPU path from import to streaming chat, stop, and restart.**

## 5. Wave 3 — Data and real SFT + LoRA

Catalyst: JSON/JSONL/CSV/Parquet/text import, instruction and conversation
mapping, preview, error rows, dedupe, normalization, deterministic split with a
recorded seed, and immutable DatasetVersion with quality report.

Yield: training draft/run/list/events/SSE/resume APIs, LLaMA Factory YAML
import/export with strict unknown-field errors, canonical TrainingSpec parameter
surface, checkpoint-backed resume rules, and preflight covering GPU, VRAM, CUDA,
model, dataset, disk, runtime, and write permission.

- [ ] **RC-W3-GATE — Real CUDA SFT + LoRA** from DatasetVersion to a PEFT adapter
  with loss, checkpoint, cancel, resume, and GPU release evidence.

## 6. Wave 4 — End-to-end delivery

- [ ] **RC-W4-01 — `BASE_PLUS_LORA` ModelVersion** with base artifact, adapter,
  canonical `adapter_config.json`, tokenizer/template inheritance, and full lineage.
- [ ] **RC-W4-02 — Send to Reactor** creates an auditable Deployment Draft;
  adapter-aware probe proves the adapter is loaded.
- [ ] **RC-W4-03 — Send to Exchange** creates an editable Route Draft; activation
  requires explicit confirmation.
- [ ] **RC-W4-04 — Gateway page** shows base URL, model ID, API key, and
  curl/Python/JavaScript snippets plus "Use in Navigator".
- [ ] **RC-W4-05 — Navigator conversation surface** bound to the Exchange route;
  a missing session surface blocks the RC.
- [ ] **RC-W4-06 — Exports** for LLaMA Factory YAML, HF/PEFT adapter package, and
  deployment manifest; no merged full weights in RC.
- [ ] **RC-W4-GATE — DatasetVersion → training → deployment → API → Navigator and
  external client** with real replies.

## 7. Public interfaces added by this plan

| Product | Interface |
| --- | --- |
| Navigator / Platform | `POST /api/v1/auth/pair`, `GET/DELETE /api/v1/auth/session`, `GET /api/v1/system/status`, credential metadata create/list/revoke, fixed forwarding prefixes, CSRF, tracing, idempotency propagation |
| Yield | `GET /api/v1/training-runs`, preflight/events/events-stream/resume actions, LLaMA Factory draft import/export |
| Reactor | ModelImport create/list/get, deployment events/observation |
| Exchange | endpoint/route lists, API keys, `/v1/models`, `/v1/chat/completions` |

All new errors use RFC 9457; mutations support idempotency keys and conflict
detection.

## 8. Test and release standard

- Automated: contracts/schemas, storage migration, idempotency, pagination,
  errors, restart recovery per Product; plugin manifest/entrypoint/DirectPlugin
  runtime/TCK/package acceptance; WebUI component/a11y/E2E; pairing expiry,
  cookie/CSRF, API-key revocation, HF token non-echo, log redaction, path
  traversal; install/upgrade/rollback/backup/restore/uninstall.
- Real environment: public and private HF import, 512-sample SFT+LoRA, metrics,
  checkpoint, cancel, resume, GPU release, adapter-aware deployment, streaming
  and revocation, second-device HTTPS, host restart consistency, upgrade without
  data loss.
- External matrix: Open WebUI, Cherry Studio, Continue against `/v1/models` and
  streaming chat.
- Delivery: capability contracts first, then Product APIs, then plugins, WebUI,
  and installer; each repository passes its exact-SHA local gate and hosted CI
  before `develop`; the Workspace locks immutable revisions for the full
  GPU/HTTPS/external acceptance and only then publishes `0.1.0-rc.1`.

## 9. After RC

RC.2 adds Echo evaluation and comparison; V1 closes feedback into Catalyst;
later milestones add WinUI, outbound relay, multi-user RBAC, multi-node
scheduling, QLoRA/full training, multimodal, and a plugin market.