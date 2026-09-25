# Cyrene Product contract v1 integration profile

Status: historical Alpha Product registry with a 2026-09-12 extraction overlay.
Historical Alpha Product SHAs remain in `product-contracts-v1.json`; exact Platform and
Plugins contract SHAs are in `contract-authorities-v1.json`. Canonical promotion
is tracked in `docs/plans/services-standardization-and-decoupling-plan.md`.

The 2026-09-08 clean-boundary follow-up removes capability payload authority
from Platform. Products obtain generic lifecycle and connection facts from
Platform, then call Plugins-owned contracts directly. `model.routing.v1` remains
a Plugins migration snapshot. Execution, model provider, dataset preparation,
training, reusable preflight, and evaluation contracts now have Plugins-owned
implementations. The old `serving.engine.v1` and `training.engine.v1` Platform
surfaces are retired rather than copied.

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
| Catalyst | `Dataset`, immutable `DatasetVersion`, lineage | Product port directly consumes Plugins-owned `dataset.preparation.v1` | Artifact bytes, Kernel operations, plugin/provider identity | `PLUGIN_CUTOVER_CANDIDATE` |
| Echo | `EvaluationSuite`, `EvaluationRun`, `EvaluationResult`, `GateDecision` | Product port directly consumes Plugins-owned `evaluation.runner.v1` | Evaluator process state, Artifact bytes, provider identity | `PLUGIN_CUTOVER_CANDIDATE` |
| Reactor | `Deployment` desired/observed state and distinct `Endpoint` | local `ServingExecutionPort` directly consumes Plugins-owned `execution.engine.v1` | Kernel/Node Agent authority, runtime evidence, model bytes, route policy | `REFERENCE_MVP_READY` |
| Exchange | external `GatewayEndpoint`, `GatewayRoute`, request/fallback policy | Plugins-owned `model.provider.v1`; migrating Plugins `model.routing.v1` snapshot | Reactor Deployment, provider/package identity, secret values | `REFERENCE_MVP_READY` |
| Navigator | ephemeral `WorkspaceSnapshot` presentation contract only | Navigator-local `ProductReadPort` | Every Product lifecycle, mutation authority, server-side source of truth | `HEADLESS_MVP_READY` |
| Yield | `TrainingRun`, `TrainingAttempt`, retry/cancellation/output lineage and `ModelVersion` | direct owner-scoped trainer, analyzer, compatibility, and dataset-validator Plugin contracts | Kernel/runtime evidence, trainer process state, Artifact bytes | `PLUGIN_CUTOVER_CANDIDATE` |

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

Selected dependencies retain their own licenses. Public repository visibility
does not replace component-level license review or create a repository-wide
relicensing grant.

| Dependency or target | Exact release / commit | Root evidence | Actual status |
|---|---|---|---|
| DuckDB | `v1.5.5` / `d8cdaa33fda8df955cc76ef58a280f68f4cd43fa` | `LICENSE`, MIT, `7e17fd31249fa875` | **Selected**, Plugins dataset-preparation implementation; Product contract remains engine-neutral |
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
- Exchange's persisted Product route calls Plugins-owned provider contracts
  directly after generic Platform package resolution. Restart, normal request,
  SSE, disabled endpoint, and unknown binding cases pass on the recorded exact
  candidate SHAs; final Azure exact-head evidence remains pending.
- Navigator proves HTTP aggregation but does not wire the parallel Tauri UI.
- Yield owns `ModelVersion` and its training lifecycle. The old
  `training.engine.v1` and `training.engine.adapter.v1` surfaces have no real
  capability consumer and are retired; the internal adapter remains a local
  application port while concrete Plugins keep owner-scoped contracts.
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
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene Product contract V1 集成配置

**状态：** 历史 Alpha Product registry，叠加 2026-09-12 提取层。历史 Alpha Product SHA 保留在 `product-contracts-v1.json`；精确 Platform 和 Plugins 契约 SHA 记录于 `contract-authorities-v1.json`。规范晋级情况由 `docs/plans/services-standardization-and-decoupling-plan.md` 跟踪。

2026-09-08 的 clean-boundary 后续工作从 Platform 移除了 capability payload 权威。Products 从 Platform 获取通用生命周期和连接事实，再直接调用 Plugins 所有的契约。`model.routing.v1` 保留为 Plugins 迁移快照。Execution、model provider、dataset preparation、training、可复用 preflight 和 evaluation 契约现在由 Plugins 提供实现。旧的 Platform `serving.engine.v1` 和 `training.engine.v1` 接口已退役，没有复制。

本配置记录各 Product 仓库之间的兼容关系和所有权，不会把任何 Product resource authority 转移给 Cyrene-Workspace。每项资源 schema 和 API 仍由 `product-contracts-v1.json` 所列的仓库拥有。

Navigator 已接受的 V1 演进记录于 [Navigator Harness authority](navigator-harness-v1.md)。下方基线表和合并 SHA registry 描述 Alpha；V1 在 DeepSeek Harness 和单一 Cyrene persistence backend 之上新增 Navigator 所有的 Conversation/AgentRun 语义。基线表中的历史事实继续保留；新 Harness 实现单独跟踪和验收，不能把基线 snapshot 阅读能力当作最终产品定位。

## Product 边界图

| Product | 持久化权威 | 可替换引擎/运行时接缝 | 明确不拥有 | MVP 证据 |
|---|---|---|---|---|
| Catalyst | `Dataset`、不可变 `DatasetVersion`、lineage | Product port 直接调用 Plugins 所有的 `dataset.preparation.v1` | Artifact bytes、Kernel operations、plugin/provider identity | `PLUGIN_CUTOVER_CANDIDATE` |
| Echo | `EvaluationSuite`、`EvaluationRun`、`EvaluationResult`、`GateDecision` | Product port 直接调用 Plugins 所有的 `evaluation.runner.v1` | Evaluator 进程状态、Artifact bytes、provider identity | `PLUGIN_CUTOVER_CANDIDATE` |
| Reactor | `Deployment` desired/observed state 和独立 `Endpoint` | 本地 `ServingExecutionPort` 直接调用 Plugins 所有的 `execution.engine.v1` | Kernel/Node Agent authority、runtime evidence、model bytes、route policy | `REFERENCE_MVP_READY` |
| Exchange | 外部 `GatewayEndpoint`、`GatewayRoute`、请求/fallback 策略 | Plugins 所有的 `model.provider.v1`；以及处于迁移中的 Plugins `model.routing.v1` snapshot | Reactor Deployment、provider/package identity、secret 值 | `REFERENCE_MVP_READY` |
| Navigator | 仅有临时 `WorkspaceSnapshot` 展示契约 | Navigator 本地 `ProductReadPort` | 所有 Product lifecycle、mutation authority、服务端真源 | `HEADLESS_MVP_READY` |
| Yield | `TrainingRun`、`TrainingAttempt`、retry/cancellation/output lineage 和 `ModelVersion` | 直接调用 owner-scoped trainer、analyzer、compatibility、dataset-validator Plugin 契约 | Kernel/runtime evidence、trainer 进程状态、Artifact bytes | `PLUGIN_CUTOVER_CANDIDATE` |

## 跨产品成功路径

```text
Catalyst DatasetVersion + Parquet ArtifactRef
    -> Yield TrainingSpec.datasetVersion
    -> Yield MODEL_ADAPTER / MODEL_CHECKPOINT ArtifactRef
    -> Echo EvaluationRun + GateDecision
    -> Reactor Deployment(modelArtifact) + Endpoint
    -> Exchange GatewayRoute(targetBindingId) + 外部模型 API
    -> Navigator 标注来源的观察视图
```

每个交接箭头上的 Artifact bytes 都留在 Artifact Plane。每个嵌套 ArtifactRef 都使用 Platform 规范 schema `contracts/schemas/manifests/artifact_ref.schema.json`；生成的 Product 副本只为 standalone OpenAPI 工具而存在，并会经过一致性检查。

## 强制边界

1. **Product Domain 不等于 Kernel Operation。** Product adapter 可私下保留执行证据，但 Product resource 不暴露这些内容，也不逐项镜像 Kernel operation state。
2. **Product State 不等于 Kernel State。** package、binding、worker、lease、PID 或 operation 单独都不能证明 Product 已就绪或成功。
3. **Artifact Plane 不等于 Product。** Products 拥有关联关系和发布决定；Artifact Plane 拥有 bytes 和内容寻址。
4. **Deployment 不等于 Endpoint。** Reactor 将二者作为不同资源拥有。Exchange 的 `GatewayEndpoint` 是第三种、用于对外发布的概念。
5. **Node Agent 不等于 Worker/Runtime/Executor。** Node Agent 和 Kernel 保留资源与监管权威；Product adapter 只请求或观察执行。
6. **Capability 不等于 Provider identity。** capability type 加不透明 binding ID 已足够。Product 代码不导入或解析 plugin package。
7. **Event 不是真源。** Event 含 resource URI/version，用来提示消费者从 owning API 回读；绝不会被当作 Product 数据库重放。

## Wire 契约与演进配置

| 主题 | 冻结的 V1 选择 | 原因 |
|---|---|---|
| HTTP 契约 | OpenAPI 3.1.2 | 兼容 JSON Schema 2020-12，且当前工具支持更广；较新的 OpenAPI 3.2.0 对这些 API 并非必需 |
| 资源 schema | JSON Schema Draft 2020-12 | `$id`、`$schema` 和相对 `$ref` namespace 稳定 |
| 错误 | RFC 9457 Problem Details | Workspace 统一 schema；生成的 Product 副本不具权威性 |
| HTTP 演进/异步/幂等 | `product-http-semantics.md` 加 RFC 9110/7240 | `Location` 始终指向 Product state，不指向 Kernel Operation |
| Event envelope | 兼容 CloudEvents 1.0.2 的结构化 JSON | 提供可互操作通知，但不建立 event-sourcing 权威 |
| Tracing | W3C Trace Context Level 1 Recommendation：`traceparent`、`tracestate` | Level 2 仍属实验性前向兼容，不是规范要求 |
| Telemetry | OpenTelemetry 稳定 HTTP conventions | 实验性的 GenAI semantic fields 不冻结到 Product 契约 |
| 分页 | 不透明的 `pageToken` / `nextPageToken` | 避免 offset 不稳定 |
| 幂等 | Cyrene 定义的 `Idempotency-Key` 语义 | IETF header 仍是 draft；V1 不冒称它已成为 RFC |

权威的 major/minor/patch、未知字段、弃用、迁移窗口和移除规则见 `product-http-semantics.md`；Products 不定义私有变体。

## 错误配置

控制 API 使用 `application/problem+json`，包含：

- RFC 9457 字段：`type`、`title`、`status`、`detail`、`instance`；
- Cyrene 字段：稳定的大写 `code`、`retryable`、源自 W3C 的 `traceId`，以及可选的 Product `resourceRef`（仅在失败状态已持久化时提供）。

既有行业数据面保留其原生错误格式：Exchange 保留兼容 OpenAI 的错误；Product 控制面错误仍使用 RFC 9457。

## Event 配置

唯一的事件 envelope 权威 `product-event.schema.json` 固定 CloudEvents 字段和最小资源变更 payload。Event `source` 指向 owning Product API，不指向 engine/provider。`data.resourceUri` 和 `data.resourceVersion` 是唯一权威交接信息；消费者必须重新获取当前资源，并能容忍版本比通知更新。

## 开源软件选择与许可证尽调

所选依赖保留各自的许可证。仓库公开可见不代表已经完成逐组件许可证审查，也不会自动授予整个仓库范围的重新许可权。

| 依赖/目标 | 精确发布版/提交 | 根许可证证据 | 实际状态 |
|---|---|---|---|
| DuckDB | `v1.5.5` / `d8cdaa33fda8df955cc76ef58a280f68f4cd43fa` | `LICENSE`，MIT，`7e17fd31249fa875` | **已选择**；Plugins 的 dataset-preparation 实现；Product contract 与引擎无关 |
| Inspect AI | `0.3.261` / `f9186b4e2f34ca81f192ae2c08535c24b7e8f356` | `LICENSE`，MIT，`c593c2afc8138852` | **未来 adapter 目标**，不是当前 Echo 依赖 |
| vLLM | `v0.28.0` / `2cf0a6915ce544dc493a0990f2ea38d81601128a` | `LICENSE`，Apache-2.0，`c71d239df91726fc` | 此 Product 分支的**未来 adapter 目标**；尚无 GPU/runtime 验收 |
| KServe | `v0.20.0` / `1fb781055dd1567164358233e1125142ca6ef1fe` | `LICENSE`，Apache-2.0，`c71d239df91726fc` | **未来 adapter 目标**；未嵌入，也不是当前运行时权威 |
| FastAPI | `0.141.1` / `95f8322ee1dcda7ceace7b1c4f6c9915b36d748f` | `LICENSE`，MIT，`4ec89ffc81485b97` | **已选 HTTP adapter**，不承载领域权威 |
| HTTPX | `0.28.1` / `26d48e0634e6ee9cdc0533996db289ce4b430177` | `LICENSE.md`，BSD-3-Clause，`4ec59d544f12b5f5` | **已选 HTTP client/test adapter** |
| Tauri | `tauri-v2.11.5` / `7cd71369c00978a3783b6ae3e9972358abbe4ae6` | Apache `0d542e0c8804e39a`、MIT `9dd42ea92cff2ede` | **未来 adapter 目标**；此 headless 分支未使用 |
| LLaMA Factory | `v0.9.5` / `7af909522a951e3ad9f022ea6f88b6755257eaa5` | `LICENSE`，Apache-2.0，`50e6751797c50ded` | **仓库现有运行时**；本契约分支不消费或修改它 |
| Apache Arrow | `apache-arrow-25.0.1` / `beccec0d0c451b7aa3e4530416ac431b3c035c69` | `LICENSE.txt` `d1c981370f1ffd4e`；`NOTICE.txt` `afd895d61a00101f` | **互操作目标**；当前 Catalyst 代码直接使用 DuckDB 写入 Parquet |
| lm-eval harness | `v0.4.13` / `ddd67220430a2470529f25fd5c05a576ca1057a0` | `LICENSE.md`，MIT，`a806e42547620dff` | **延后的 Inspect AI 替代方案**；避免形成两套 evaluator 集成权威 |
| YARP | `v2.3.0` / `4154b6d1eaa0712597e3482f817b400b55268cae` | `LICENSE.txt`，MIT，`cfc21f5e8bd655ae` | **已延后**；现在嵌入会重复现有 Exchange route/state authority |

Pydantic 2.13.5（MIT）、Uvicorn 0.52.4（BSD-3-Clause）、JSON Schema 4.26.0（MIT）、Ruff 0.16.5（MIT）、mypy 2.3.1（MIT）、pytest 9.1.1（MIT）、Hatchling 1.32.0（MIT）和 OpenAPI Spec Validator 0.9.0（Apache-2.0）均为 package manager 依赖或验证工具，不属于复用的引擎代码。

AGPL、SSPL、BSL、Commons Clause、source-available 和许可证未知的候选项被策略拒绝，没有被采用。未向这些分支复制外部源码。

## 已知偏差与验收缺口

- 六个 Product checkout 的根目录都缺少 `LICENSE` 文件，尽管数个 package manifest 声称 Apache-2.0。对外再分发前，maintainer/legal 必须确认各仓库的许可证权威。
- Catalyst、Echo 和 Navigator 均在其精确且同步的 `main` 和 `develop` SHA 上通过专属 Product 检查。
- Reactor 的真实 Product lifecycle 使用本地 reference process。现有 vLLM runtime adapter 仍需通过此 Product port 完成 GPU/model 端到端验证。
- Reactor 的 Rust 1.96/PyO3 Clippy 回归已在未增加宽泛 lint waiver 的情况下修正；Python CI 现在会先安装声明的 runtime package。生产 vLLM/KServe 和 GPU/model 端到端仍是未来 adapter 工作。
- Echo 的确定性引擎已真实运行，但此切片尚未实现隔离的 Inspect AI adapter。
- Exchange 持久化 Product route 在通用 Platform package resolution 后，直接调用 Plugins 所有的 provider contract。记录中的精确候选 SHA 已通过重启、普通请求、SSE、disabled endpoint 和 unknown binding 场景；最终 Azure exact-head 证据仍待补齐。
- Navigator 证明了 HTTP aggregation，但尚未接入并行的 Tauri UI。
- Yield 拥有 `ModelVersion` 和训练生命周期。旧 `training.engine.v1`、`training.engine.adapter.v1` 没有真实 capability consumer，已退役；内部 adapter 仍是本地 application port，具体 Plugins 保留 owner-scoped contract。
- Yield 的 `training-unit` workflow 会解析明确指定的 canonical Platform checkout，并将精确 SDK 路径传入子 worker。嵌套 worker 取消和持久恢复采用 fail-closed；所需的 hosted Platform/Official Plugins 集成会实际运行，不跳过凭据检查。
- SQLite MVP 跨重启持久化终态、失败证据和幂等映射。要通过 HA 验收，仍需生产数据库/controller 实现命令中途崩溃协调及多副本命令的原子保留。
- Product 通知名称及 CloudEvents 兼容的通用 envelope 已冻结，但没有 MVP 声称实现 transactional outbox 或 broker publisher。可靠的 at-least-once 持久发布以及 replay/retention 策略仍属集成工作。
