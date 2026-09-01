# Cyrene Product contract v1 integration profile

Status: frozen contract candidate, 2026-08-31.

This profile records compatibility and ownership across Product repositories. It
does not move any Product resource authority into Cyrene-Workspace. Every
resource schema and API remains owned by the repository listed in
`product-contracts-v1.json`.

## Product boundary map

| Product | Durable authority | Replaceable engine/runtime seam | Explicit non-ownership | MVP evidence |
|---|---|---|---|---|
| Catalyst | `Dataset`, immutable `DatasetVersion`, lineage | `data.processor.v1` | Artifact bytes, Kernel operations, plugin/provider identity | SQLite restart + real DuckDB JSONL→Parquet + persisted parse failure |
| Echo | `EvaluationSuite`, `EvaluationRun`, `EvaluationResult`, `GateDecision` | `evaluation.runner.v1` | Evaluator process state, Artifact bytes, provider identity | SQLite restart + real JSONL evaluation + independent PASS/FAIL gate + persisted malformed-input failure |
| Reactor | `Deployment` desired/observed state and distinct `Endpoint` | `serving.engine.v1` | Kernel/Node Agent authority, model bytes, route policy | SQLite restart + real subprocess/HTTP inference + identity reconciliation + stop/retire + unsupported-model failure |
| Exchange | external `GatewayEndpoint`, `GatewayRoute`, request/fallback policy | existing `model.routing.v1` and `model.provider.v1` seams | Reactor Deployment, provider/package identity, secret values | SQLite restart + persisted planner + real gateway/upstream HTTP + disabled-route failure |
| Navigator | ephemeral `WorkspaceSnapshot` presentation contract only | `ProductReader` HTTP/client SPI | Every Product lifecycle, mutation authority, server-side source of truth | real HTTP fan-out + raw owner payload + trace propagation + partial/unconfigured failure |
| Yield | `TrainingRun`, `TrainingAttempt`, retry/cancellation/output lineage | `training.engine.adapter.v1` contract | Kernel lease/fence, trainer process state, Artifact bytes | contract and SPI only; no runtime claim due parallel ownership |

## Cross-product happy path

```text
Catalyst DatasetVersion + Parquet ArtifactRef
    -> Yield TrainingSpec.datasetVersion
    -> Yield MODEL_ADAPTER / MODEL_CHECKPOINT ArtifactRef
    -> Echo EvaluationRun + GateDecision
    -> Reactor Deployment(modelArtifact) + Endpoint
    -> Exchange GatewayRoute(targetBindingId) + external model API
    -> Navigator source-labelled observed views
```

Artifact bytes remain in the Artifact Plane at every arrow. Products persist
only immutable references, digests, lineage, and their own resource state.

## Hard boundaries

1. **Product Domain != Kernel Operation.** Product resources may retain opaque
   execution references but never mirror Kernel operation state one-for-one.
2. **Product State != Kernel State.** A package, binding, worker, lease, PID, or
   operation cannot alone prove Product readiness or success.
3. **Artifact Plane != Product.** Products own relationships and publication
   decisions; the Artifact Plane owns bytes and content addressing.
4. **Deployment != Endpoint.** Reactor owns both as distinct resources.
   Exchange's `GatewayEndpoint` is a third, external publication concept.
5. **Node Agent != Worker/Runtime/Executor.** Node Agent and Kernel keep resource
   and supervision authority; Product adapters only request/observe execution.
6. **Capability != Provider identity.** Capability type plus an opaque binding id
   is sufficient. Product code does not import or parse plugin packages.
7. **Event != source of truth.** Events contain resource URI/version and prompt a
   read from the owning API. They are never replayed as the Product database.

## Wire and evolution profile

| Concern | Frozen v1 choice | Rationale |
|---|---|---|
| HTTP contracts | OpenAPI 3.1.2 | JSON Schema 2020-12 compatibility and broader current tooling; OpenAPI 3.2.0 is newer but not required for these APIs |
| Resource schemas | JSON Schema Draft 2020-12 | Stable `$id`, `$schema`, and relative `$ref` namespaces |
| Errors | RFC 9457 Problem Details | Stable `code`, `retryable`, `traceId`, optional `resourceRef` extensions |
| Async creates/cancel | RFC 7240 `Prefer: respond-async`, HTTP 202 + `Location` | Durable Product resource remains separate from execution operation |
| Event envelope | CloudEvents 1.0.2-compatible structured JSON | Interoperable notification without event-sourcing authority |
| Tracing | W3C Trace Context `traceparent` | Cross-Product correlation with no custom trace identity |
| Telemetry | OpenTelemetry stable HTTP conventions | Experimental GenAI semantic fields are not frozen into Product contracts |
| Pagination | opaque `pageToken` / `nextPageToken` | Avoid offset instability |
| Idempotency | Cyrene-defined `Idempotency-Key` semantics | The IETF header remains a draft, so v1 does not mislabel it as an RFC |

Within v1, fields may be added and optional enum behavior may be documented.
Removing a field/enum member, changing meaning/state authority, or adding a
required input requires a new major API version. JSON Schema `$id` values and
resource URLs remain stable for the life of v1.

## Error profile

Control APIs emit `application/problem+json` with:

- RFC 9457: `type`, `title`, `status`, `detail`, `instance`;
- Cyrene: stable uppercase `code`, `retryable`, W3C-derived `traceId`, and
  optional Product `resourceRef` when failure state was persisted.

Existing industry data planes keep their native error shape: Exchange preserves
OpenAI-compatible errors. Product control failures remain RFC 9457.

## Event profile

The common `product-event.schema.json` pins CloudEvents fields and a minimal
resource-change payload. The event `source` identifies the owning Product API,
not an engine/provider. `data.resourceUri` and `data.resourceVersion` are the
only authority handoff; consumers fetch the current resource and tolerate a
newer version than the notification.

## OSS selection and license due diligence

All selected or deferred candidates are permissively licensed. The actual root
license/NOTICE file was read at the exact tag, and the abbreviated SHA-256 below
is the content evidence captured during review.

| Candidate | Exact release / commit | Root evidence | Decision and reuse strategy |
|---|---|---|---|
| DuckDB | `v1.5.5` / `d8cdaa33fda8df955cc76ef58a280f68f4cd43fa` | `LICENSE`, MIT, `7e17fd31249fa875` | **Selected**, Catalyst embedded adapter; contract remains engine-neutral |
| Inspect AI | `0.3.261` / `f9186b4e2f34ca81f192ae2c08535c24b7e8f356` | `LICENSE`, MIT, `c593c2afc8138852` | **Selected adapter target**, Echo; execute untrusted tasks in an isolated runtime, not Product process |
| vLLM | `v0.28.0` / `2cf0a6915ce544dc493a0990f2ea38d81601128a` | `LICENSE`, Apache-2.0, `c71d239df91726fc` | **Selected existing Reactor engine target**; no GPU acceptance claimed here |
| KServe | `v0.20.0` / `1fb781055dd1567164358233e1125142ca6ef1fe` | `LICENSE`, Apache-2.0, `c71d239df91726fc` | **Selected external controller candidate**; not embedded and not Product state authority |
| FastAPI | `0.141.1` / `95f8322ee1dcda7ceace7b1c4f6c9915b36d748f` | `LICENSE`, MIT, `4ec89ffc81485b97` | **Selected HTTP adapter**, not domain authority |
| HTTPX | `0.28.1` / `26d48e0634e6ee9cdc0533996db289ce4b430177` | `LICENSE.md`, BSD-3-Clause, `4ec59d544f12b5f5` | **Selected HTTP client/test adapter** |
| Tauri | `tauri-v2.11.5` / `7cd71369c00978a3783b6ae3e9972358abbe4ae6` | Apache `0d542e0c8804e39a`, MIT `9dd42ea92cff2ede` | **Selected Navigator shell target**; no parallel UI changes in this branch |
| LLaMA Factory | `v0.9.5` / `7af909522a951e3ad9f022ea6f88b6755257eaa5` | `LICENSE`, Apache-2.0, `50e6751797c50ded` | **Selected existing Yield engine**, contract/SPI only |
| Apache Arrow | `apache-arrow-25.0.1` / `beccec0d0c451b7aa3e4530416ac431b3c035c69` | `LICENSE.txt` `d1c981370f1ffd4e`; `NOTICE.txt` `afd895d61a00101f` | **Selected interchange family**; Parquet MVP produced through DuckDB |
| lm-eval harness | `v0.4.13` / `ddd67220430a2470529f25fd5c05a576ca1057a0` | `LICENSE.md`, MIT, `a806e42547620dff` | **Deferred alternative** to Inspect AI; avoid two evaluator integration authorities |
| YARP | `v2.3.0` / `4154b6d1eaa0712597e3482f817b400b55268cae` | `LICENSE.txt`, MIT, `cfc21f5e8bd655ae` | **Deferred**; embedding now would duplicate current Exchange route/state authority |

Pydantic 2.13.5 (MIT), Uvicorn 0.52.4 (BSD-3-Clause), JSON Schema
4.26.0 (MIT), Ruff 0.16.5 (MIT), mypy 2.3.1 (MIT), pytest 9.1.1
(MIT), Hatchling 1.32.0 (MIT), and OpenAPI Spec Validator 0.9.0
(Apache-2.0) are package-manager dependencies or validation tools, not reused
engine code.

AGPL, SSPL, BSL, Commons Clause, source-available, and unknown-license
candidates are policy-rejected and were not consumed. No external source was
copied into these branches.

## Known deviations and acceptance gaps

- Root `LICENSE` text is absent in several Product checkouts despite package
  metadata claiming Apache-2.0. Maintainers/legal must establish repository
  license authority before external redistribution.
- Catalyst, Echo, and Navigator entered from contract/legacy baselines with
  manifest-only CI. Their branches add dedicated real-test workflows; those
  checks still need remote execution before merge acceptance is complete.
- Reactor's real Product lifecycle uses a local reference process. The existing
  vLLM runtime adapter still needs GPU/model E2E through this Product port.
- Reactor's untouched repository-wide strict Clippy gate currently fails in
  `components/scheduler-rs/src/topology.rs` under Rust 1.96. Product checks,
  Cargo check/test, and the correctly hydrated local Python baseline pass. The
  existing remote Python job also omits its protobuf dependency and therefore
  fails collection at `test_proto_roundtrip.py`. These parallel-owned baseline
  issues are not changed in this slice.
- Echo's deterministic engine is real but the Inspect AI isolated adapter is not
  implemented in this slice.
- Exchange's persisted route adapter is exercised with real HTTP and also
  passes the canonical resolver/Official connector path locally at Platform
  `f46190e0f5fed19ff43d6805e5d5823ae1b77557` and Plugins
  `78b47bf1ffcf3fed83e0db36507b5cbbe8574a2e`; remote cross-repository CI still
  needs to reproduce that exact-SHA gate.
- Navigator proves HTTP aggregation but does not wire the parallel Tauri UI.
- Yield is contract/SPI only. Filesystem-path internal specs and the current
  lifecycle-shaped `training.engine.v1` compatibility seam remain explicit
  migration work.
- Yield's untouched `training-unit` workflow attempts `pip install -e
  training/core`, but that directory has neither `pyproject.toml` nor
  `setup.py`. The new contract workflow passes; the existing parallel-owned CI
  baseline remains blocked.
- The SQLite MVPs persist terminal state, failure evidence, and idempotency
  mappings across restart. Mid-command crash reconciliation and atomic
  multi-replica command reservation require a production database/controller
  implementation before HA acceptance.
- Product notification names and the common CloudEvents-compatible envelope are
  frozen, but no MVP claims a transactional outbox or broker publisher. Durable
  at-least-once publication and replay/retention policy remain integration work.
