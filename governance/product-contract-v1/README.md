# Cyrene Product contract v1 integration profile

Status: canonical Alpha contract registry, 2026-09-05. The exact merged Product
and Platform `main` SHAs are recorded in the registry JSON files.

The 2026-09-08 clean-boundary follow-up freezes `model.routing.v1`,
`serving.engine.v1`, and `training.engine.v1` as
`MIGRATING_COMPATIBILITY`. New integrations use generic capability descriptors,
CES payload forwarding, and Product-owned replaceable ports.

This profile records compatibility and ownership across Product repositories. It
does not move any Product resource authority into Cyrene-Workspace. Every
resource schema and API remains owned by the repository listed in
`product-contracts-v1.json`.

Navigator's accepted V1 evolution is recorded in
[Navigator Harness authority](navigator-harness-v1.md). The baseline table and
merged SHA registry below describe Alpha; V1 adds Navigator-owned Conversation
and AgentRun semantics over DeepSeek Harness and one Cyrene persistence backend.

Navigator 的 V1 边界演进见上述文档；下表保留 Alpha 历史事实，新 Harness 实现与验收
单独跟踪，不把基线快照读取能力当作最终产品定位。

## Product boundary map

| Product | Durable authority | Replaceable engine/runtime seam | Explicit non-ownership | MVP evidence |
|---|---|---|---|---|
| Catalyst | `Dataset`, immutable `DatasetVersion`, lineage | canonical candidate `data.processor.v1`; local `DataProcessingPort` | Artifact bytes, Kernel operations, plugin/provider identity | `REFERENCE_MVP_READY` |
| Echo | `EvaluationSuite`, `EvaluationRun`, `EvaluationResult`, `GateDecision` | canonical candidate `evaluation.runner.v1`; local `EvaluationExecutionPort` | Evaluator process state, Artifact bytes, provider identity | `REFERENCE_MVP_READY` |
| Reactor | `Deployment` desired/observed state and distinct `Endpoint` | migrating `serving.engine.v1`; local `ServingExecutionPort`; generic CES target | Kernel/Node Agent authority, runtime evidence, model bytes, route policy | `REFERENCE_MVP_READY` |
| Exchange | external `GatewayEndpoint`, `GatewayRoute`, request/fallback policy | migrating `model.routing.v1`; experimental `model.provider.v1`; generic CES target | Reactor Deployment, provider/package identity, secret values | `REFERENCE_MVP_READY` |
| Navigator | ephemeral `WorkspaceSnapshot` presentation contract only | Navigator-local `ProductReadPort` | Every Product lifecycle, mutation authority, server-side source of truth | `HEADLESS_MVP_READY` |
| Yield | `TrainingRun`, `TrainingAttempt`, retry/cancellation/output lineage | migrating `training.engine.v1`; local `TrainingEngineAdapter` port; generic CES target | Kernel/runtime evidence, trainer process state, Artifact bytes | `CONTRACT_CANDIDATE_READY` |

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

Artifact bytes remain in the Artifact Plane at every arrow. Every nested
ArtifactRef consumes the canonical Platform schema at
`contracts/schemas/manifests/artifact_ref.schema.json`; generated Product copies
exist only for standalone OpenAPI tooling and are conformance-checked.

## Hard boundaries

1. **Product Domain != Kernel Operation.** Product adapters may retain execution
   evidence privately, but Product resources never expose it or mirror Kernel
   operation state one-for-one.
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
| Errors | RFC 9457 Problem Details | One Workspace schema; generated Product copies are non-authoritative |
| HTTP evolution/async/idempotency | `product-http-semantics.md` + RFC 9110/7240 | `Location` always names Product state, never Kernel Operation |
| Event envelope | CloudEvents 1.0.2-compatible structured JSON | Interoperable notification without event-sourcing authority |
| Tracing | W3C Trace Context Level 1 Recommendation: `traceparent`, `tracestate` | Level 2 remains experimental forward compatibility, not normative |
| Telemetry | OpenTelemetry stable HTTP conventions | Experimental GenAI semantic fields are not frozen into Product contracts |
| Pagination | opaque `pageToken` / `nextPageToken` | Avoid offset instability |
| Idempotency | Cyrene-defined `Idempotency-Key` semantics | The IETF header remains a draft, so v1 does not mislabel it as an RFC |

The authoritative major/minor/patch, unknown-field, deprecation, migration
window, and removal rules are in `product-http-semantics.md`; Products do not
define private variants.

## Error profile

Control APIs emit `application/problem+json` with:

- RFC 9457: `type`, `title`, `status`, `detail`, `instance`;
- Cyrene: stable uppercase `code`, `retryable`, W3C-derived `traceId`, and
  optional Product `resourceRef` when failure state was persisted.

Existing industry data planes keep their native error shape: Exchange preserves
OpenAI-compatible errors. Product control failures remain RFC 9457.

## Event profile

The sole event-envelope authority, `product-event.schema.json`, pins CloudEvents
fields and a minimal resource-change payload. The event `source` identifies the
owning Product API, not an engine/provider. `data.resourceUri` and
`data.resourceVersion` are the only authority handoff; consumers fetch the
current resource and tolerate a newer version than the notification.

## OSS selection and license due diligence

All selected or deferred candidates are permissively licensed. The actual root
license/NOTICE file was read at the exact tag, and the abbreviated SHA-256 below
is the content evidence captured during review.

| Dependency or target | Exact release / commit | Root evidence | Actual status |
|---|---|---|---|
| DuckDB | `v1.5.5` / `d8cdaa33fda8df955cc76ef58a280f68f4cd43fa` | `LICENSE`, MIT, `7e17fd31249fa875` | **Selected**, Catalyst embedded adapter; contract remains engine-neutral |
| Inspect AI | `0.3.261` / `f9186b4e2f34ca81f192ae2c08535c24b7e8f356` | `LICENSE`, MIT, `c593c2afc8138852` | **FUTURE ADAPTER TARGET**, not a current Echo dependency |
| vLLM | `v0.28.0` / `2cf0a6915ce544dc493a0990f2ea38d81601128a` | `LICENSE`, Apache-2.0, `c71d239df91726fc` | **FUTURE ADAPTER TARGET** for this Product branch; no GPU/runtime acceptance |
| KServe | `v0.20.0` / `1fb781055dd1567164358233e1125142ca6ef1fe` | `LICENSE`, Apache-2.0, `c71d239df91726fc` | **FUTURE ADAPTER TARGET**, not embedded or current runtime authority |
| FastAPI | `0.141.1` / `95f8322ee1dcda7ceace7b1c4f6c9915b36d748f` | `LICENSE`, MIT, `4ec89ffc81485b97` | **Selected HTTP adapter**, not domain authority |
| HTTPX | `0.28.1` / `26d48e0634e6ee9cdc0533996db289ce4b430177` | `LICENSE.md`, BSD-3-Clause, `4ec59d544f12b5f5` | **Selected HTTP client/test adapter** |
| Tauri | `tauri-v2.11.5` / `7cd71369c00978a3783b6ae3e9972358abbe4ae6` | Apache `0d542e0c8804e39a`, MIT `9dd42ea92cff2ede` | **FUTURE ADAPTER TARGET**; not used by this headless branch |
| LLaMA Factory | `v0.9.5` / `7af909522a951e3ad9f022ea6f88b6755257eaa5` | `LICENSE`, Apache-2.0, `50e6751797c50ded` | **EXISTING REPOSITORY RUNTIME**, not consumed or modified by this contract branch |
| Apache Arrow | `apache-arrow-25.0.1` / `beccec0d0c451b7aa3e4530416ac431b3c035c69` | `LICENSE.txt` `d1c981370f1ffd4e`; `NOTICE.txt` `afd895d61a00101f` | **INTERCHANGE TARGET**; the current Catalyst code directly uses DuckDB to write Parquet |
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

- Root `LICENSE` text is absent in all six Product checkouts despite several
  package manifests claiming Apache-2.0. Maintainers/legal must establish each
  repository's license authority before external redistribution.
- Catalyst, Echo, and Navigator have dedicated Product checks passing on their
  exact, synchronized `main` and `develop` SHAs.
- Reactor's real Product lifecycle uses a local reference process. The existing
  vLLM runtime adapter still needs GPU/model E2E through this Product port.
- Reactor's Rust 1.96/PyO3 Clippy regression is corrected without a broad lint
  waiver, and its Python CI now installs the declared runtime package before
  testing. Production vLLM/KServe and GPU/model E2E remain future adapter work.
- Echo's deterministic engine is real but the Inspect AI isolated adapter is not
  implemented in this slice.
- Exchange's persisted Product route is exercised through canonical Platform
  resolver/CES delegation and the Official worker with a real upstream HTTP
  result. Restart, normal request, SSE, disabled endpoint, and unknown binding
  cases pass on the recorded exact SHAs.
- Navigator proves HTTP aggregation but does not wire the parallel Tauri UI.
- Yield is contract candidate only. `training.engine.adapter.v1` was removed as
  a duplicate capability; the internal adapter is a local application port.
  Filesystem-path internal specs and the current lifecycle-shaped
  `training.engine.v1` compatibility seam remain explicit migration work.
- Yield's `training-unit` workflow resolves an explicit canonical Platform
  checkout and propagates that exact SDK path into child workers. Nested worker
  cancellation and durable recovery fail closed, and the required hosted
  Platform/Official Plugins integration executes without credential skips.
- The SQLite MVPs persist terminal state, failure evidence, and idempotency
  mappings across restart. Mid-command crash reconciliation and atomic
  multi-replica command reservation require a production database/controller
  implementation before HA acceptance.
- Product notification names and the common CloudEvents-compatible envelope are
  frozen, but no MVP claims a transactional outbox or broker publisher. Durable
  at-least-once publication and replay/retention policy remain integration work.
