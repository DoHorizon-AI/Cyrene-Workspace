# Cyrene Text Model Lifecycle V1

> **中文主计划 / Primary plan:** 把 Cyrene 从基础设施基线推进为一套可以真实使用的、开放而不强制绑定的文本模型生命周期工具。本文是持续更新的实施计划与验收清单；实现完成度必须通过稳定检查 ID 和证据记录表达。
>
> **English summary:** Cyrene V1 is an open, independently usable lifecycle across Catalyst, Yield, Reactor, Exchange, Navigator, and Echo. The first delivery is limited to text LLMs on Linux NVIDIA CUDA nodes, with DeepSeek Harness adoption proof before Navigator replaces its current generic-runtime direction. Product authority stays in Cyrene; OSS components remain replaceable engines or plugins.

## 0. Document control / 文档控制

| Field | Value |
| --- | --- |
| Plan ID | CYRENE-TEXT-LIFECYCLE-V1 |
| Current status | IMPLEMENTING — 16 scoped Phase 0 component checks have Actual PASS evidence; P0-GATE and the remaining checks remain pending |
| Baseline authority | Workspace origin/main at eb5fb2cf1af5c3dd0cbadbe6167bc388b643e318 (2026-09-05); Product contract registry remains under governance/product-contract-v1/ |
| Implementation line | User-authorized implementation proceeds from the Workspace main baseline toward Develop; this plan is maintained on the task branch |
| Runtime scope | Linux execution node, NVIDIA GPU, CUDA; Windows is a supported desktop client target for Navigator adoption proof |
| Model scope | Text LLM only; first training path is SFT + LoRA/PEFT using the existing LLaMA Factory runtime |
| Serving scope | vLLM as the first Reactor serving engine |
| Harness proof pin | dsh-v0.1.3-alpha.1 / d347e703908d0406b7a7ef80e3a0e594d86b2215 |
| Upstream reference | [DeepSeek Harness pinned tree](https://github.com/deepseek-ai/deepseek-harness/tree/d347e703908d0406b7a7ef80e3a0e594d86b2215) |
| Progress | 16 scoped Phase 0 checks passed; P0-GATE, the remaining Phase 0 checks and all later phase gates remain pending |

### 0.1 What this document is and is not / 本文边界

本文把目标拆成可执行工作和可复核证据，不声称当前代码已经实现这些功能。用户已经授权开始实施；后续每个代码变更都必须更新对应检查 ID 的证据。

当前 Workspace 的主分支是基础设施和 Product contract 的全绿 baseline。全绿只代表已声明的仓库检查在其覆盖范围内通过，不代表真实 CUDA 训练、真实模型加载、真实桌面安装包、真实远程恢复或完整生命周期已经通过。本计划把这些真实行为单独列为 Actual PASS。

### 0.2 Baseline observations / 当前基线观察

以下是启动计划时使用的主分支事实。它们是工作起点和缺口说明，不是新功能 PASS：

| Area | Current evidence / 当前证据 | Consequence / 对计划的影响 |
| --- | --- | --- |
| Workspace | README.md、repositories.yaml 和 Product contract registry 提供多仓库拓扑与边界 | 所有跨仓库资源必须通过 canonical contract、ArtifactRef 或 Product API 交接 |
| Catalyst | 当前有 Dataset/DatasetVersion、JSONL 到 Parquet 的 DuckDB 参考处理端口和本地 CAS；完整清洗、字段映射、review、deduplicate、split、反馈整理仍需产品化 | Phase 2/3 必须先把可审查的 DatasetVersion 和可导出的训练输入做实 |
| Yield | 已有 TrainingSpec、LLaMA Factory adapter、preflight/tiny-dry-run、checkpoint/output lineage 与本地生命周期模型；默认执行路径及 Kernel live authority 尚未证明真实 CUDA 训练 | 不能把 fake executor、tiny checkpoint 或 compatibility seam 当作 real CUDA acceptance |
| Reactor | 有 vLLM 相关 runtime adapter、Deployment/Endpoint 产品端口和 reference server；真实模型下载、GPU load、健康检查和推理链路仍需 E2E | Phase 1 必须以真实模型首次推理作为 ready 条件 |
| Exchange | 已有持久化 route、Platform resolver/CES worker 连接和流式基础；第一版协议仍需端到端补齐 tools、tool results、cancel、usage、认证与路由证据 | Navigator adoption proof 必须经过 Exchange 的完整 tool continuation；普通 chat 不足以通过 |
| Navigator | 当前产品端主要是 headless/read-only Workspace snapshot；历史 Tauri/Rust 客户端拥有可复用的 session、agent、import 能力，但不是新 authority | Phase 0 必须建立 DeepSeek Harness Profile/Bundle、Cyrene persistence backend 和新的 Conversation/AgentRun 产品语义 |
| Echo | 有本地 SQLite、EvaluationSuite/Run/Result/GateDecision 和确定性 JSONL evaluator；真实模型/会话评估与 Inspect adapter 仍需接入 | Phase 3 保持用户选择 feedback，禁止自动把所有失败样本送回训练 |
| Platform | 有 Execution Fabric、Node/Runtime Agent、Placement、Lease/Fence、Artifact Transfer、Workspace Fabric 等通用基础 | Platform 继续拥有设备、执行、Artifact authority；产品不得另建 GPU scheduler 或资源真源 |
| Plugins | Official catalog 已有 provider/agent/runtime 契约方向，但训练与 serving engine catalog 仍需实际绑定和发布 | 先用成熟现有 runtime；新增插件必须有 allowlist、版本和来源证据 |

历史 Alpha 契约把 Navigator 的 MVP 表述为 WorkspaceSnapshotPresentation。V1 的已接受边界演进记录在 [Navigator Harness authority](../../governance/product-contract-v1/navigator-harness-v1.md)：Navigator 拥有 Conversation 和 AgentRun 产品语义。原 registry 中已合并提交的历史证据保持可追溯，新实现单独验收。

## 1. Product outcome / 最终产品结果

Cyrene V1 的第一条真实用户路径是：用户把数据整理为 DatasetVersion，使用 NVIDIA CUDA 节点训练文本模型，得到可部署的 ModelVersion，启动 vLLM Endpoint，通过 Exchange 使用模型，在 Navigator 中进行 Chat/Agent/Tool 会话，再把选中的会话反馈交给 Echo 评估和 Catalyst 整理，开始下一轮训练。

~~~text
Catalyst
数据导入 / 预览 / 整理 / DatasetVersion
    │ Send to
    ▼
Yield
TrainingSpec / CUDA SFT LoRA / Checkpoint / ModelVersion
    │ Send to
    ▼
Reactor
Deployment / vLLM / Endpoint
    │ Send to
    ▼
Exchange
Route / Chat API / Streaming / Tool Calls / Usage
    │ Open in
    ▼
Navigator
DeepSeek Harness Profile / Conversation / AgentRun / Approval
    │ Send to
    ▼
Echo
Evaluation / Compare / Feedback selection
    │ Send to
    ▼
Catalyst
DatasetVersion v2
~~~

这是开放闭环，不是封闭流水线。每个产品都必须可以单独进入、单独运行、单独导出：

~~~text
Import     外部资源进入产品
Export     产品资源以可携带格式离开
Open in    在另一个产品中打开只读引用或当前资源
Send to    用户明确触发一次带资源引用的交接
~~~

任何箭头都可以跳过、替换或由外部工具承担：

| Product | Owns / 拥有 | Can enter independently / 可独立进入 | Can leave independently / 可独立退出 |
| --- | --- | --- | --- |
| Catalyst | Dataset、DatasetVersion、Source/lineage、Schema、Split、QualityReport、TransformationRecipe、TrainingCandidate | Import JSONL/Parquet/外部 DatasetVersion | Export 标准 JSONL/Parquet/训练候选与 lineage |
| Yield | TrainingRun、TrainingSpec、TrainingAttempt、Checkpoint、Training Result、ModelVersion | Import DatasetVersion 或外部 JSONL/模型基座 | Export TrainingSpec、checkpoint、merged model、ModelVersion manifest |
| Reactor | Deployment、Loaded Model 观察、Serving lifecycle、Endpoint | 直接部署外部 Hugging Face ModelVersion/模型目录 | Export Endpoint 连接信息、Deployment manifest、运行状态与日志引用 |
| Exchange | Route、API Credential、Usage、Quota、Request routing、protocol compatibility | 连接 Reactor Endpoint 或任意受支持外部 Provider/Endpoint | Export OpenAI-compatible endpoint/config、usage/audit reports |
| Navigator | Conversation、AgentRun、Tool Approval、Session import/share、客户端体验 | 经过 Exchange、直接外部 Provider、导入外部 Harness 会话 | Export conversation/session/分享引用；Send to Echo/Catalyst |
| Echo | EvaluationRun、EvaluationSuite、EvaluationResult、Comparison、GateDecision、FeedbackSet | 导入会话、模型、Endpoint、外部结果 | Export 评估报告与用户选择的 FeedbackSet |

Navigator adoption proof 的模型请求必须经过 Exchange，以证明 Cyrene 的统一认证、路由、tool call continuation 和 usage 链路；这不改变 Navigator 未来可以直接连接外部 Provider 的产品能力。外部 Provider 连接是 Navigator 的合法独立入口，Exchange 是 proof 和 Cyrene 托管模型的统一入口。

## 2. Authority and architecture / 权威边界与整体架构

### 2.1 Product authority / 产品权威

| Authority | Owns / 拥有 | Must not own / 不应拥有 |
| --- | --- | --- |
| DeepSeek Harness | Agent Loop、Tool Call lifecycle、Session event model、通用 Approval/Skill/Workflow/Plugin runtime、主要 Harness UI 基础 | Cyrene 的组织/身份真源、GPU/Artifact authority、第二套可写 Session 数据库 |
| Navigator | Conversation、AgentRun、客户端共享体验、会话导入、审批呈现、团队/多设备恢复语义、Send to 交互 | DatasetVersion、TrainingRun、Deployment、Route、Platform Operation 的生命周期真源 |
| Cyrene Workspace / Identity | User、Organization、Workspace membership、device identity、SSO/OIDC、RBAC、plugin governance | Agent loop、训练器内部状态、模型推理协议的业务实现 |
| Exchange | Model routing、Provider authentication、Gateway API、Usage/Quota/Cost/Audit、protocol adaptation | Reactor Deployment、Provider package identity、secret plaintext、Conversation message history |
| Platform | Device enrollment、GPU/resource discovery、Placement、Execution、Lease/Fence、Artifact bytes/transfer | Product lifecycle、Conversation、Route、Dataset/Training/Evaluation semantics |
| Catalyst | Dataset、DatasetVersion、lineage、schema/split/quality/recipe、TrainingCandidate、feedback整理 | Trainer process、GPU authority、ModelVersion bytes 的运行时加载 |
| Yield | TrainingRun、TrainingSpec、Checkpoint、Training Result、ModelVersion publication decision | GPU scheduler、Node/Lease authority、trainer process authority、Artifact bytes 本身 |
| Reactor | Deployment、Loaded Model、serving lifecycle、Endpoint | GPU/Node authority、model bytes、gateway route policy、Conversation |
| Echo | EvaluationSuite、EvaluationRun、EvaluationResult、Comparison、GateDecision、FeedbackSet | Dataset/Training/Deployment authority、会话写入真源、provider secrets |

边界规则：

1. Product domain state 与 Platform Kernel operation state 分离。PID、Lease、worker、package binding 或单个事件不能单独证明 Product ready/success。
2. Artifact bytes 与 Product metadata 分离。跨产品传递优先使用 canonical ArtifactRef、manifest、resource URI/version；如果目标产品需要离线使用，必须支持携带可验证的导出包。
3. Deployment、Endpoint、GatewayEndpoint 是三个不同资源。Reactor 决定部署和 Endpoint；Exchange 决定对外 Gateway publication；Navigator 只消费经过授权的来源。
4. Node Agent、Runtime Agent、worker、executor 是不同角色。产品通过 Platform port 请求执行，不另造 GPU authority。
5. 跨产品 CloudEvents 是通知，消费者根据 resourceUri 和 resourceVersion 回读 owning API。Harness Session events 则是本产品的持久化记录模型，由单一 Cyrene persistence backend 按序保存；这两种 event 不混为同一 authority。
6. Navigator 的显示 Token estimate 可以是估算；真实 Usage、Cost、Quota 和归属以 Exchange 的请求记录为准。

### 2.2 Planes / 平面划分

~~~text
┌──────────────────────────────────────────────────────────────────────┐
│ Product plane                                                         │
│ Catalyst Dataset │ Yield Training │ Reactor Serving │ Echo Evaluation │
│ Exchange Route/Usage │ Navigator Conversation/AgentRun               │
└───────────────┬───────────────────────────────┬──────────────────────┘
                │ references / commands         │ events are notifications
┌───────────────▼───────────────────────────────▼──────────────────────┐
│ Common platform plane                                                │
│ Workspace / Identity · Device · Placement · Execution · Lease/Fence  │
│ ArtifactRef / transfer / integrity · audit and trace context          │
└───────────────┬───────────────────────────────┬──────────────────────┘
                │                                │
       ┌────────▼────────┐              ┌────────▼────────┐
       │ Harness plane   │              │ Model data plane │
       │ DeepSeek Profile│              │ vLLM/Exchange    │
       │ + Cyrene session│              │ Chat/Tool/Usage  │
       └─────────────────┘              └─────────────────┘
~~~

共享资源选择器只选择已加入 Workspace 且当前用户可见、可用的 Compute/Artifact/Endpoint；它不把六个产品合成一个控制面板。各产品可以在没有其它产品在线时继续 Import、Export、Open in 或使用外部资源。

### 2.3 Canonical resource handoff / 资源交接

优先复用 Workspace/Platform 已有的 schema 和 API，不为每个产品复制一套 ID：

| Resource | Canonical owner | Handoff rule |
| --- | --- | --- |
| ArtifactRef、artifact manifest、integrity、lineage | Platform Artifact plane | 使用 artifact://sha256/<hex> 和完整 digest/size/kind；目标产品回读并校验，不把 URI 当作拥有 bytes |
| DatasetVersion、training candidate | Catalyst | Yield 引用 immutable version；处理 recipe、source、split、quality report 必须可以追溯 |
| TrainingSpec、TrainingRun、Checkpoint、ModelVersion | Yield | ModelVersion 包含 base model、adapter/merged weights、tokenizer/chat template、runtime compatibility 和 lineage |
| Deployment、Endpoint | Reactor | ready 只在真实模型加载并成功推理后成立；Endpoint 不能代替 Deployment |
| GatewayRoute、GatewayEndpoint、Usage | Exchange | Navigator proof 所有托管模型请求从此进入；外部 Provider 仍可作为 Navigator 独立连接 |
| Conversation、AgentRun、Session events | Navigator + Cyrene persistence backend | Harness 事件是 session 记录模型；Navigator 产品元数据另存，message history 不得有第二个可写真源 |
| EvaluationRun、EvaluationResult、FeedbackSet | Echo | 评估输出可导出；只有用户明确选择的 feedback 才交给 Catalyst |

每一个 Send to 动作都应记录：来源资源 URI/version、目标动作、发起用户、权限决定、输入 artifact digest、目标 Product response URI 和 idempotency key。交接失败必须保留可诊断的 RFC 9457 Problem Details 或产品原生数据面错误。

## 3. Progress and evidence protocol / 完成度与证据协议

### 3.1 Three levels of completion / 三种完成状态

每个检查 ID 都必须分别记录以下三层状态：

| Level | Meaning / 含义 | Sufficient to tick [x]? |
| --- | --- | --- |
| CODE_COMPLETE | 实现、配置、迁移或打包产物已落盘，并且 code review 可定位 | No |
| LOCAL_TEST_PASS | 在目标仓库声明的本地环境执行了有针对性的测试/静态检查 | No |
| ACTUAL_PASS | 在真实跨进程、真实硬件、真实安装包、真实身份或真实恢复场景完成了该 ID 的验收 | Yes, only with complete evidence |

单元测试、fake peer、fake executor、模拟 GPU、仅有 HTTP 200、仅有 package binding、仅能在开发机启动，都不能单独产生 ACTUAL_PASS。如果某项不需要真实硬件，必须在它的验收说明中写明可替代的真实边界。

### 3.2 Evidence record / 证据记录

勾选任何检查 ID 时，在同一检查项下或链接的验收报告中填写全部字段。空字段禁止 [x]：

~~~text
Evidence for <CHECK-ID>
- Commit / PR: <exact commit, PR, or immutable artifact digest>
- Commands: <exact commands, configuration, and test selection>
- Result: <pass/fail, counts, endpoint/session/model identifiers>
- Environment: <OS, CUDA, driver, GPU/VRAM, runtime versions, identity topology>
- Limitations: <known gaps, skipped scenarios, credential/external-service blockers>
~~~

证据至少应包含：

- exact commit/PR 或不可变制品 digest；不能只写 branch 名或 latest；
- 完整命令、配置、测试选择和退出结果；
- 真实环境，包括 OS、CUDA、driver、GPU/VRAM、模型/数据 artifact、服务拓扑；
- 结果中的资源 ID、Session ID、Deployment/Endpoint、Usage 或 trace ID；
- 限制、失败、重试、未覆盖项以及是否为环境阻塞；
- 如果依赖远程 CI，区分 LOCAL_ACCEPTANCE、REMOTE_CI_BLOCKED 和 CANONICAL_PENDING。

### 3.3 Update rules / 更新规则

1. 代码落盘时只更新该 ID 的 CODE_COMPLETE，不勾选主复选框。
2. 局部测试通过时只写 LOCAL_TEST_PASS 和命令结果，不把测试名改成 PASS 来掩盖集成缺口。
3. 只有 Actual PASS 的真实证据完整后才把 [ ] 改为 [x]，同时填写日期、环境和限制。
4. 一个阶段的总项只能在该阶段所有必选 ID 和阶段 gate 都通过后勾选。
5. 代码变化、上游 pin 变化、环境变化或证据失效时，回退对应检查为 [ ]，在限制字段解释原因。
6. 失败和阻塞不勾选；用 BLOCKED 或 FAILED 状态和证据记录说明。禁止用 continue-on-error、跳过测试、fake lifecycle 或静默 fallback 制造绿色结果。

### 3.4 Known environment evidence / 已知环境证据

本机 WSL2 已能访问 NVIDIA RTX 5070 12 GB，并可运行真实 Windows PowerShell。DeepSeek Harness 精确源码、pnpm 11.7.0 frozen install 和 Linux official build 已完成；npm 上尚未发布该预发布版本，因此使用固定源码构建。上游来源审计返回 Core patches = 0。

真实 vLLM 0.25.1 已加载固定 revision 的 Qwen3-4B-Instruct-2507 并通过 health；这只是 Phase 0 的外部 Provider 环境，还不是 Reactor / Platform 部署验收。WSL 使用 loopback rendezvous、V1 Model Runner、非 FlashInfer sampler 和带开发头文件的 Python 3.12.11，未修改 vLLM 源码。

当前组件验证：Python persistence 全仓测试 13 passed；真实上游 TypeScript、Rust stdio、Codex import/continue 和独立服务重启和 Web Profile 集成 17 passed / 0 failed / 0 skipped；独立 Native Rust 18 passed。真实 Web Profile 已完成模型→Rust Tool→模型续答，并保存、列出、恢复和服务重启后的 Session；新的实时流证据包含 119 个 assistant frames、87 个文本增量和持久化事件。取消子场景已完成真实停止、终止事件和恢复验证，但 P0-11 所要求的持久化 Usage/Auth/Audit ledger 仍未完成。双进程接管来自同机独立进程，不能替代第二台物理设备；Windows 安装包和桌面启动仍未 Actual PASS。当前证据见 [Phase 0 evidence, 2026-09-06](evidence/2026-09-06-phase-0.md)，旧记录见 [历史证据, 2026-09-05](evidence/2026-09-05-phase-0.md)。

## 4. Phase 0 — Navigator adoption proof / Navigator 采用证明

### Objective / 目标

回答一个可验证的问题：**DeepSeek Harness 能否在不维护第二套 Session 真源、不过度修改上游 Core 的前提下，成为 Navigator V1 的 upstream Agent Runtime、Session、Tool、Plugin 和主要交互基础。**

Phase 0 通过前，Navigator 仍保留当前实现作为对照；Phase 0 通过后，停止继续自研通用 Agent Loop、Session engine、Tool lifecycle 和 plugin manager，只实现 DeepSeek Harness 缺失或不适合企业场景的差异化能力。

### 4.1 Fixed upstream / 固定上游

- [x] **P0-01 — Exact upstream pin:** 锁定 dsh-v0.1.3-alpha.1 和 commit d347e703908d0406b7a7ef80e3a0e594d86b2215，记录 package lock、构建工具链、依赖 digest、Bundle 版本和来源。参见 [固定来源证据](evidence/2026-09-06-phase-0.md) 与 [历史固定来源证据](evidence/2026-09-05-phase-0.md)。
- [ ] **P0-02 — Reproducible upstream build:** 在 Linux 开发环境和 Windows packaging 环境分别按 lock 重建；能证明依赖没有漂移，失败时 fail closed。
- [x] **P0-03 — Core patch audit:** 对上游 Core 变更做 immutable diff 审计，目标为 UPSTREAM_CORE_PATCHES = 0。若存在 patch，必须记录理由、影响、upstreamability 和移除条件。2026-09-06 固定源码审计结果为 0，参见 [Phase 0 evidence](evidence/2026-09-06-phase-0.md)。
- [x] **P0-04 — Compatibility ledger:** 记录上游 Session v2、SessionHandle、单写者锁、flush 语义、历史加载性能限制和已知兼容性风险；没有验证过的上游承诺不得写成 Cyrene guarantee。参见 [兼容性台账](evidence/2026-09-06-phase-0.md#compatibility-ledger)。

### 4.2 Profile / Bundle / plugin policy

- [x] **P0-05 — Navigator Profile:** 在 cyrene-navigator 中建立独立 Profile/Bundle，固定上游 runtime、UI extension、配置覆盖入口和构建产物布局。Linux 真实启动的默认 preset 为 `cyrene-navigator`，参见 [Profile 与 Bundle 证据](evidence/2026-09-06-phase-0.md#profile-and-bundle)。
- [x] **P0-06 — Bundle inventory:** Phase 0 固定并审计实际加载的插件、来源、版本和 disabled defaults；企业动态插件允许列表、签名、管理员策略和版本治理属于 P4-13，不提前当作 adoption proof 的前提。参见 [实际 Bundle inventory](evidence/2026-09-06-phase-0.md#profile-and-bundle)。
- [x] **P0-07 — Disabled defaults:** 通过配置关闭不适合 Navigator 的默认本地 session log、未经 Exchange 管理的默认路由、未治理的遥测或插件入口；关闭行为要有启动日志/诊断证据，不能只隐藏 UI。Profile 诊断已记录 JSONL persistence、telemetry、默认 DeepSeek route 和 title LLM defaults 为 disabled；企业 allowlist 仍属于 P4。
- [ ] **P0-08 — Cyrene extension boundary:** Profile 只注册 Exchange、Workspace/Identity、persistence、usage/audit、conversation import、native bridge、Send to 和 UI extension；不把 Cyrene Product 生命周期塞回 Harness Core。

### 4.3 Exchange path / Exchange 路径

adoption proof 的模型请求必须经过 Exchange。Navigator 直接连接外部 Provider 是后续和日常使用允许的独立能力，但不能替代 proof 的 Exchange path。

- [x] **P0-09 — Exchange streaming chat:** Harness 发起真实 streaming chat，请求、首 token、结束、错误和 usage 都能关联同一 request/trace ID。真实 Exchange proof 的帧、增量、请求 ID、usage 和终止事件见 [streaming evidence](evidence/2026-09-06-phase-0.md#exchange-streaming-and-tool-continuation)。
- [x] **P0-10 — Exchange tool continuation:** 完成真实 Tool Call → Tool execution → Tool Result → model continuation；顶层 tools、tool choice、structured tool arguments、result correlation、stream events 在 Gateway 全链路保真。真实 `cy-manifest` Tool 与匹配的 Tool Result 已由模型消费并续答，参见 [streaming/tool evidence](evidence/2026-09-06-phase-0.md#exchange-streaming-and-tool-continuation)。
- [ ] **P0-11 — Exchange cancel/auth/usage:** 真实取消能停止下游生成并返回可判断的状态；认证、权限、token/usage 记录和失败审计可回读；取消后不得重试为另一条模型请求。
  当前证据已通过真实取消子场景：下游停止、`user-aborted` 终止事件、取消后 vLLM 无排队/运行请求、同 Session 恢复和无 retry/no fake usage 检查；但 Exchange 的持久化 Usage/Auth/Audit Product ledger 尚未证明，因此整项保持 `[ ]`，参见 [取消子场景证据](evidence/2026-09-06-phase-0.md#cancellation-sub-scenario)。
- [ ] **P0-12 — External Provider isolation (tracked in P1-12):** Navigator 能用配置独立连接一个外部 Provider，并明确标记其不经过 Exchange 的来源、usage owner 和权限边界；该入口不会改变 Cyrene persistence 和 Navigator session authority。此项是 V1 独立使用要求，不阻塞 Phase 0 的 Exchange adoption proof。

### 4.4 Cyrene persistence / Cyrene 会话持久化

目标是让 Harness 的 Session event model 写入 Cyrene persistence backend。Harness 本地数据库不能与 Cyrene 服务端数据库形成两套可写 message history 真源。

- [x] **P0-13 — Persistence contract adapter:** 实现并版本化上游 SessionPersistence / SessionHandle 所需的 Cyrene backend adapter；保存原生 event、schema/version、source metadata 和 resource identity。参见 [持久化证据](evidence/2026-09-06-phase-0.md#cyrene-persistence-and-recovery) 与 [历史接口证据](evidence/2026-09-05-phase-0.md)。
- [x] **P0-14 — Append-only ordering:** 事件按序追加，支持 single writer、ordered append、idempotent batch/retry 和 flush barrier；append 成功与 durable flush 的语义分开记录。参见 [持久化证据](evidence/2026-09-06-phase-0.md#cyrene-persistence-and-recovery) 与 [历史持久化证据](evidence/2026-09-05-phase-0.md)。
- [x] **P0-15 — Restart recovery:** 重启 Harness 后能恢复 Session、Conversation projection、AgentRun 状态和未完成 turn 的正确边界；恢复只读取事件，不重新执行历史 Tool。第二个 Web process 接管、服务重启后的恢复和后续新 turn 已通过，参见 [恢复证据](evidence/2026-09-06-phase-0.md#cyrene-persistence-and-recovery)。
- [x] **P0-16 — Persistence service restart:** 单独重启 persistence service 后 Session 内容和 writer ownership 不丢失；重启期间的失败可诊断且不会产生两个 writer。参见 [service kill/restart 证据](evidence/2026-09-06-phase-0.md#cyrene-persistence-and-recovery) 与 [历史证据](evidence/2026-09-05-phase-0.md)。
- [ ] **P0-17 — Multi-device read/takeover:** 第二客户端设备使用经授权的 Workspace 身份读取会话并在明确 takeover 后取得 writer；旧 writer 的 append 被拒绝并返回可诊断冲突。Phase 0 可用受控 service credentials 证明边界，完整组织 RBAC/OIDC 仍由 P4-08/P4-09 验收。
  现有接管证据来自同机的第二个独立 Web process，不能满足“第二客户端设备”的完整文字标准；因此保持 `[ ]`，参见 [恢复证据的拓扑限制](evidence/2026-09-06-phase-0.md#cyrene-persistence-and-recovery)。
- [ ] **P0-18 — Product metadata separation:** 证明 Navigator metadata 与单一可写 event log 的边界，owner/workspace 可审计；title、sharing policy、tags、permissions 和 audit projection 的完整产品体验在后续企业阶段继续实现，不复制可写 message history。
- [ ] **P0-19 — Crash and replay boundary:** 模拟 append 前后崩溃、重复 batch、断线恢复和读取较新 resourceVersion，证明事件重放不会执行历史工具，也不会丢失或重排对话。

### 4.5 Rust native bridge / Rust 原生能力接入

保留现有 Rust 能力，不改写为 TypeScript，不引入 Node Native Addon，不把固定监听端口当成默认依赖：

~~~text
DeepSeek Harness
      ↓
thin Cordis Plugin
      ↓ inherited stdio / versioned IPC
Rust binary or cyrene-native-host
~~~

- [x] **P0-20 — IPC protocol:** 定义并实现 protocol version、request id、method、params、result、error、cancel、event；stdout 只承载协议，stderr 承载日志。Rust stdio 集成与协议测试通过，参见 [Rust bridge 证据](evidence/2026-09-06-phase-0.md#rust-stdio-bridge)。
- [ ] **P0-21 — Process lifecycle:** Cordis plugin 能启动、握手、取消、超时、回收、重启 Rust child；异常退出能映射为可见 Tool error，不把僵尸进程留给桌面端。
- [x] **P0-22 — Real Rust tool:** 使用一个已有 Rust binary（优先 cy-manifest 或等价 Platform artifact utility）作为真实 Tool，模型实际调用并消费结果，不使用内存 fake。真实 `cy-manifest` 返回 Artifact manifest 后由模型续答，参见 [真实 Rust Tool 证据](evidence/2026-09-06-phase-0.md#rust-stdio-bridge)。
- [ ] **P0-23 — Rust error/cancel:** 验证非法参数、binary 不存在、stderr 日志、超时、用户取消、child crash、协议版本不兼容和大结果边界。
- [ ] **P0-24 — Portability:** Linux 与 Windows 都能通过 inherited stdio 工作；没有固定监听端口要求；启动环境、路径、编码和权限由证据记录。

### 4.6 External session import / 外部会话导入

- [x] **P0-25 — Real Codex fixture:** 导入至少一个真实 Codex 会话样本，保留来源标识、原始内容 hash/bytes、转换报告、分支关系和不支持内容说明。真实 Codex CLI 0.152.1 样本的来源字段、bytes/hash 和 conversion report 已保留在 event log，参见 [Codex import 证据](evidence/2026-09-06-phase-0.md#codex-import-and-safety)。
- [x] **P0-26 — Import safety:** 导入的历史 tool call、approval、permission 和命令只作为历史事件/显示数据，不自动执行、不获得当前设备权限、不写入新的执行队列。历史记录均为 `executable: false`，unknown content 保留且不触发执行，参见 [Codex import safety](evidence/2026-09-06-phase-0.md#codex-import-and-safety)。
- [ ] **P0-27 — Resume semantics:** 导入会话能作为只读历史打开，并在用户明确建立新的 AgentRun 后继续；新 turn 的权限、模型和 Workspace 来源重新判定。
  真实后端 Continue 已创建带新 `cwd` 和 preset 的新 AgentRun，历史 Tool 未执行；桌面端的 Open/Continue 交互仍未验收，故保持 `[ ]`。

### 4.7 Windows desktop proof / Windows 桌面证明

优先复用现有 Tauri shell 承载 DeepSeek Harness Web UI 和 Cyrene UI extension；桌面壳不成为第二个 Session 真源。

- [ ] **P0-28 — Reproducible package:** 生成可安装 Windows desktop package，包含锁定版本的 Harness/Profile、native host 和必要运行时；不依赖源码开发环境。
- [ ] **P0-29 — First launch:** 在干净 Windows 环境安装、启动、退出和再次启动；网络、证书、配置目录、日志目录和升级失败可诊断。
- [ ] **P0-30 — Desktop chat/tool:** 安装包完成 Exchange chat、streaming、真实 Rust Tool、approval、错误和取消。
- [ ] **P0-31 — Desktop recovery:** 关闭/重启桌面端后从 Cyrene persistence 恢复 Session；验证 persistence service 重启、网络断开和旧 writer 拒绝。
- [ ] **P0-32 — Dynamic local transport:** 若 Harness UI server 使用本地 HTTP，仅使用系统分配的 loopback 临时端口或等价受监管通道；不写死服务端口，不要求手工端口配置。

### 4.8 Phase 0 gate / Phase 0 总门槛

- [ ] **P0-GATE — Adoption proof PASS:** 固定上游与 Profile/Bundle、真实 Exchange streaming/chat/tool continuation、单一 Cyrene persistence、Harness/service 重启、第二客户端读取与 writer fencing、真实 Rust Tool、Codex 导入及 Windows 安装包均通过；UPSTREAM_CORE_PATCHES = 0 或每个例外都有完整移除记录。P0-12 在 Phase 1 验收，P0-06/P0-17/P0-18 的企业扩展在 Phase 4 验收，不把后续范围提前扩成 adoption 前提。

只有 P0-GATE 通过，才正式停止 Navigator 通用 Harness Runtime 自研路线。若 adoption proof 失败，记录失败边界后再决定是补 Cyrene extension、向上游提交可移植改进，还是保留当前 Navigator 作为过渡；不能悄悄 fork 成不可升级的第二套 Core。

## 5. Phase 1 — Model consumption / 模型消费闭环

### Objective / 目标

先让用户可以使用一个已有的普通文本模型：ModelVersion/外部模型 → Reactor → vLLM → Endpoint → Exchange → Navigator。第一版用一张 NVIDIA GPU 完成真实消费路径；多节点资源体验放到 Phase 4。

- [ ] **P1-01 — Model fixture lock:** 选择一个可再分发、可验证的文本模型 fixture，锁定 revision、tokenizer、chat template、precision、context limit、license 和目标 GPU/VRAM；可评估的候选是 Qwen/Qwen3-4B-Instruct-2507，但在验收前必须写入最终配置。
- [ ] **P1-02 — External model import:** Reactor 能直接导入/引用外部 Hugging Face 模型或本地可验证模型目录，不依赖 Yield 在线存在。
- [ ] **P1-03 — Compute selection:** Reactor 使用 Platform resource/placement port 选择一张已加入 Workspace 的 NVIDIA GPU；产品不保存一套独立 SSH/IP/GPU authority。
- [ ] **P1-04 — Real vLLM launch:** 通过 Platform execution 启动真实 vLLM，传递 CUDA/device binding、model artifact、tokenizer/chat template 和受控 runtime config。
- [ ] **P1-05 — Load readiness:** Deployment 只有在 vLLM 真实加载目标模型、完成健康检查并成功返回一次推理后才可报告 ready；reference server 或 served:<content> 不能作为此项证据。
- [ ] **P1-06 — Endpoint lifecycle:** Endpoint 与 Deployment 分离，完成 create/start/stop/restart/failed-load/cleanup，观察状态与实际进程状态不互相冒充。
- [ ] **P1-07 — Exchange route:** Exchange 能把 GatewayRoute 指向该 Endpoint，处理认证、模型名映射、超时、不可用和错误；route 不拥有 Reactor Deployment。
- [ ] **P1-08 — Chat and streaming:** 真实客户端完成普通 chat、streaming、finish reason、prompt/completion usage；usage 来源和估算/真实值区分。
- [ ] **P1-09 — Tool call path:** 真实 vLLM/模型/Exchange/Navigator 完成 tool call、tool result 和模型续答；如果模型或 backend 不支持，必须明确报 capability error，不静默降级为纯文本。
- [ ] **P1-10 — Cancellation:** Navigator 或 Exchange 取消请求后，vLLM generation、Gateway stream 和客户端 UI 都进入一致的终态；不会错误 fallback 到另一个 endpoint。
- [ ] **P1-11 — Open in Navigator:** Reactor/Exchange 可以通过 Open in 或 Send to 把 Endpoint/Route 交给 Navigator；Navigator 显示来源、权限和模型路由信息。
- [ ] **P1-12 — External-provider independence:** Navigator 可在不创建 Reactor Deployment 的情况下连接一个外部 Provider；该路径拥有独立的 provider credential、usage source 和错误边界。
- [ ] **P1-GATE — Daily model consumption PASS:** 一张 NVIDIA GPU 上从 external model 或 ModelVersion 到 Navigator chat 的真实路径通过 deploy、health、streaming、tool、cancel、usage、stop、restart；所有证据完整。

## 6. Phase 2 — Real training / 真实训练闭环

### Objective / 目标

把 Catalyst 的可审查 DatasetVersion 交给 Yield，在 Linux NVIDIA CUDA 节点上使用现有 LLaMA Factory 完成真实 SFT + LoRA/PEFT，产生可部署的 ModelVersion，再回到 Reactor。

### 6.1 Catalyst dataset path / Catalyst 数据路径

- [ ] **P2-01 — Dataset import:** 导入 JSONL/Parquet/标准外部数据，保留 source、digest、schema、license/provenance 和原始 artifact。
- [ ] **P2-02 — Preview and mapping:** 提供可检查 preview、字段映射、消息格式验证、错误行报告和可重复 TransformationRecipe。
- [ ] **P2-03 — Transform and quality:** 支持清洗、deduplicate、质量检查、敏感/无效样本标记和人工 review；失败行不能静默丢弃。
- [ ] **P2-04 — Deterministic split:** 生成可重复的 train/validation split，记录 seed、规则、输入 digest 和样本计数；评估 holdout 不得被训练自动覆盖。
- [ ] **P2-05 — DatasetVersion publish:** 只有完成处理、review、integrity 和 lineage 后才发布 immutable DatasetVersion，并生成 canonical ArtifactRef。
- [ ] **P2-06 — Dataset export/send:** 可 Export 标准训练文件，或通过 Send to Yield 传递 DatasetVersion URI/version、ArtifactRef、recipe、split 和 quality report；Yield 不写回 Catalyst 数据库。

### 6.2 Yield training path / Yield 训练路径

- [ ] **P2-07 — TrainingSpec contract:** 明确 model base revision、dataset version、SFT format、LoRA/PEFT params、precision、sequence length、batch/accumulation、output policy、compute selector 和 cancellation policy。
- [ ] **P2-08 — Canonical compute admission:** Yield 通过 Platform training.engine.v1/Product adapter 请求 Compute、Placement、Execution、Lease；不启用私有 GPU scheduler，不从 CUDA_VISIBLE_DEVICES 推断资源所有权。
- [ ] **P2-09 — Real CUDA preflight:** 在目标节点验证 driver、CUDA、GPU UUID/VRAM、Python/runtime、model/dataset access、disk 和 write permission；preflight 失败要在 TrainingRun 中持久化。
- [ ] **P2-10 — Real LLaMA Factory SFT LoRA:** 使用锁定版本的 LLaMA Factory/现有 Yield adapter 启动真实 CUDA SFT + LoRA/PEFT；必须有真实 GPU utilization、进程、loss/progress 和 artifact evidence。
- [ ] **P2-11 — Training lifecycle:** TrainingRun/Attempt 正确表示 queued/running/progress/failed/cancelling/cancelled/completed/awaiting_retry；Product state 与 Kernel operation、PID、worker state 分离。
- [ ] **P2-12 — Cancel and release:** 用户取消能使真实训练进程停止、保存终态、释放 Lease/GPU 和写出 failure/partial artifact 说明；只设置 cancel flag 或杀本地 wrapper 不算通过。
- [ ] **P2-13 — Checkpoint and resume:** 训练中产生可校验 Checkpoint，断开控制服务或重启后能按明确 attempt/resume policy 继续，不能重复发布相同 ModelVersion 或损坏 artifact。
- [ ] **P2-14 — Export and ModelVersion:** 完成 adapter/merged export、tokenizer/chat template/model manifest、base/dataset/checkpoint lineage 和 integrity；明确 QLoRA/adapter 是否需要 base model 才能部署。
- [ ] **P2-15 — Reactor handoff:** 通过 Send to Reactor 部署真实 ModelVersion；Reactor 只在模型真实加载/推理后 ready。
- [ ] **P2-16 — No simulated acceptance:** 把现有 tiny dry-run/fake executor 作为开发回归即可；本阶段 Actual PASS 必须来自 NVIDIA CUDA、真实 LLaMA Factory process、真实 checkpoint、真实 restart/resume 和真实 inference。
- [ ] **P2-GATE — Real training PASS:** 从 Catalyst DatasetVersion 到 Yield CUDA SFT LoRA，再到 checkpoint/resume/export/ModelVersion/Reactor inference 的链路真实通过；所有输入输出 digest、GPU、命令、状态和限制均有证据。

## 7. Phase 3 — Feedback loop / 反馈闭环

### Objective / 目标

把 Navigator 的真实 Conversation/AgentRun 有选择地交给 Echo，完成确定性或 Inspect evaluator、比较和失败分析；用户明确选择的 FeedbackSet 才送回 Catalyst 形成 DatasetVersion v2。

- [ ] **P3-01 — Conversation export:** Navigator 能 Export 带 source、model/route、tool/approval events、usage、timestamps、session/version 和 provenance 的会话包。
- [ ] **P3-02 — Send to Echo:** Send to Echo 创建 EvaluationRun 引用 Conversation、Endpoint/ModelVersion、suite、evaluator config 和 input artifact，不复制或拥有原会话。
- [ ] **P3-03 — Evaluation runner adapter:** Echo 通过自己的 EvaluationExecutionPort 接入确定性 evaluator，并可选接入 Inspect AI adapter；evaluator 进程状态不冒充 Echo EvaluationRun authority。
- [ ] **P3-04 — Real evaluation:** 对真实 Conversation/Endpoint/ModelVersion 执行 expected score、custom scorer、trace/tool correctness 或 judge；记录每个样本日志、usage、错误和可复现配置。
- [ ] **P3-05 — Comparison:** 比较不同 ModelVersion、Endpoint、Route 或 EvaluationRun，显示样本级差异、质量指标、成本/usage 和失败类别。
- [ ] **P3-06 — Failure analysis:** 用户能查看失败样本、原始输入/输出、tool event、judge explanation 和 provenance；任何自动裁剪或脱敏都可见且可重现。
- [ ] **P3-07 — Feedback selection:** 用户在 Echo 中选择/修订/标注 FeedbackSet；GateDecision 不自动等于“送回训练”。
- [ ] **P3-08 — Catalyst feedback import:** Send to Catalyst 只发送选中的 FeedbackSet 与原始/修订数据引用，保留 Echo EvaluationResult、选择者、时间和原因。
- [ ] **P3-09 — DatasetVersion v2:** Catalyst 根据 feedback 生成新 DatasetVersion v2，重新 preview、quality、deduplicate、split 和 review；训练 holdout 与 evaluation holdout 明确隔离。
- [ ] **P3-10 — V1/V2 loop:** Yield 训练 v2，Reactor 部署 v2，Echo 对 v1/v2 做同一套 suite comparison；用户能作出是否发布/继续迭代的决定。
- [ ] **P3-GATE — User-selected feedback loop PASS:** 至少两轮真实会话/模型比较完成；feedback 经过用户选择后才进入 DatasetVersion v2；v1/v2 评估与训练 holdout 保持可追溯且没有自动全量回流。

## 8. Phase 4 — Multi-device and enterprise / 多设备与企业能力

### Objective / 目标

让 GPU 设备加入 Workspace 一次后，被 Yield、Reactor 和其它有权限的产品通过同一个资源选择器使用；补齐生产身份、团队、usage、插件治理和恢复能力。

~~~text
My RTX 4090              Online / Idle / 24 GB
Office GPU Server        Online / Training / RTX 6000

Yield    Compute: Auto | My RTX 4090 | Office GPU Server
Reactor  Compute: Auto | My RTX 4090 | Office GPU Server
~~~

- [ ] **P4-01 — Workspace enrollment:** 设备通过 Workspace/Platform enrollment 加入一次，生成 device identity、capability snapshot、owner/workspace membership 和 revoke path。
- [ ] **P4-02 — Two-node inventory:** 至少两台真实 NVIDIA CUDA Linux execution node 能被发现、显示 GPU UUID/VRAM/CUDA/driver/状态，并能区分 offline、idle、training、serving 和 unknown。
- [ ] **P4-03 — Shared resource picker:** Yield 与 Reactor 使用同一个授权资源选择器；用户可以选 Auto 或明确设备，产品不要求分别填写 SSH/IP/API server。
- [ ] **P4-04 — Placement and lease:** Platform 为真实训练/serving 创建正确的 placement/lease/fence；同一张 exclusive GPU 的冲突、队列、释放和失败重试有可观察状态。
- [ ] **P4-05 — Remote execution:** Yield 在远程 GPU 节点完成真实 CUDA training，Reactor 在另一台或同一台节点完成真实 vLLM serving；control reachability 与 inference endpoint reachability 分别证明。
- [ ] **P4-06 — Offline/reconnect:** Node 或 control connection 短暂断开后，运行状态、artifact transfer、lease 和恢复策略符合声明；不能把未知状态伪装成 completed/running。
- [ ] **P4-07 — Artifact portability:** 训练产物、模型、数据和评估包可以通过 Artifact plane 跨节点转移、断点续传、校验和原子发布；坏包被拒绝。
- [ ] **P4-08 — OIDC/SSO:** Workspace 用户、组织、设备和 service identity 分离；OIDC/SSO 登录、过期、撤销和多组织边界有真实证据。
- [ ] **P4-09 — RBAC:** Dataset、TrainingRun、Deployment、Route、Conversation、EvaluationRun、device 和 plugin 权限按 Workspace/Organization/user/service account 校验；默认拒绝。
- [ ] **P4-10 — Team sharing:** Navigator Conversation/session sharing、takeover、审计和撤销可用；共享不建立第二份可写 message history。
- [ ] **P4-11 — Usage/cost ownership:** Exchange 将请求、tokens、latency、provider/model/route、cost estimate/actual cost 与用户/组织/Workspace 关联；Navigator 和其它产品读取而不改写 Exchange usage 真源。
- [ ] **P4-12 — API tokens/quota:** 为第三方程序和内部服务提供 scoped token、过期/撤销、quota/rate limit、错误和审计；token plaintext 不进入 Product metadata 或客户端日志。
- [ ] **P4-13 — Plugin governance:** 企业 allowlist、版本锁定、来源/hash、权限声明、启停、升级/回滚和不兼容诊断可用；Navigator Bundle 不能任意加载未审查插件。
- [ ] **P4-14 — Backup/recovery:** Product metadata、Cyrene persistence、Artifact references、Workspace membership、route/usage audit 和恢复点有备份/恢复演练；恢复后 authority 和 writer 不分叉。
- [ ] **P4-15 — Operational readiness:** 关键指标、trace、audit、告警、升级、凭据轮换、失败重试和数据保留策略经过目标环境演练。
- [ ] **P4-GATE — Multi-device enterprise PASS:** 两台以上真实 GPU 节点、Workspace enrollment、共同资源选择器、远程训练/serving、OIDC/SSO、RBAC、usage/cost/audit、plugin governance 与备份恢复全部获得 Actual PASS。

## 9. Cross-cutting acceptance / 横向质量门槛

这些检查贯穿所有阶段；它们不替代阶段 gate，但任何一项缺失都可以阻止最终 V1 宣布可用。

- [ ] **X-01 — Contract alignment:** Product contract registry、Navigator Conversation/AgentRun authority、ArtifactRef、ModelVersion、DatasetVersion、Endpoint、GatewayRoute 和事件 envelope 的 owner/版本/兼容规则一致。
- [ ] **X-02 — Independent product operation:** Catalyst、Yield、Reactor、Exchange、Navigator、Echo 各自能在缺少其它产品时完成其声明的 Import/Export/独立入口；失败只在真正需要的 handoff 边界暴露。
- [ ] **X-03 — No hidden binding:** 产品没有默认硬编码另一个产品的数据库、SSH、IP、provider secret、PID、训练路径或本地 session store；所有跨产品依赖可由配置/授权/资源引用观察。
- [ ] **X-04 — Idempotency and ordering:** 创建、Send to、Import、Export、cancel、retry、resume、publish 和 route mutation 遵守 idempotency、resourceVersion 和 ordering 语义。
- [ ] **X-05 — Failure honesty:** service restart、node disconnect、bad artifact、model load failure、permission revoke、quota、tool crash、provider timeout、client crash 和 stale writer 都有可判定终态或明确 unknown 状态。
- [ ] **X-06 — Security boundary:** secrets、tokens、session events、tool approvals、device grants、artifact tickets 和 audit logs 的权限、加密/传输、脱敏、保留和撤销规则经过检查。
- [ ] **X-07 — Reproducibility:** 上游 engine、model、dataset、plugin、CUDA/runtime、container/package、config 和命令都能被 exact version/digest 重建；不存在未记录的 latest。
- [ ] **X-08 — Observability:** 每个跨产品操作可用 resource ID、operation/attempt/session/request/trace ID 关联；日志、指标、审计和用户可见错误不会互相矛盾。
- [ ] **X-09 — Export portability:** 每个产品的 Export 可以被同一产品重新 Import 或由声明的外部工具消费；导出包包含 schema/version/integrity/provenance 和限制说明。
- [ ] **X-10 — License and redistribution:** 要打包或发布的上游/插件/模型/运行时 license、NOTICE、依赖来源和再分发限制已审计；许可证缺口不能被 CI green 覆盖。

## 10. Final user journey / 最终用户验收

最终 V1 的成功条件是用户在声明的真实环境中亲自完成完整路径，并且每个箭头可替换或跳过。以下是最终演练的稳定检查 ID：

- [ ] **V1-01 — Catalyst import:** Import 外部 JSONL/Parquet，看到 preview、schema、mapping、quality、split、lineage。
- [ ] **V1-02 — Dataset publish:** 发布 immutable DatasetVersion 和 ArtifactRef，Export 结果并校验 digest。
- [ ] **V1-03 — Send to Yield:** 明确发送 DatasetVersion，选择 Workspace Compute，看到 TrainingSpec/TrainingRun。
- [ ] **V1-04 — Train:** 在真实 NVIDIA CUDA 节点执行 SFT + LoRA，看到真实 progress/loss 和 GPU utilization。
- [ ] **V1-05 — Checkpoint/resume:** 训练中 checkpoint，取消或重启后 resume，最终生成可验证 ModelVersion。
- [ ] **V1-06 — Send to Reactor:** 部署 ModelVersion 到 vLLM，选择 GPU，看到真实 model load、health 和 Endpoint。
- [ ] **V1-07 — Send to Exchange:** 创建 route，完成认证、streaming chat、tool call/result、cancel 和 usage。
- [ ] **V1-08 — Open in Navigator:** 在 Navigator 中打开 route/endpoint，使用 DeepSeek Harness Profile 完成 Chat/Agent/Approval/Rust Tool。
- [ ] **V1-09 — Session recovery/share:** 重启 Navigator/persistence service，在第二设备读取或 takeover，同一 Session 无第二可写 message history。
- [ ] **V1-10 — Send to Echo:** 把真实 Conversation/Model/Endpoint 交给 Echo，运行 evaluator、查看样本级结果和 comparison。
- [ ] **V1-11 — Feedback to Catalyst:** 用户选择 feedback 后送回 Catalyst，生成 DatasetVersion v2；未选择内容不会自动进入训练。
- [ ] **V1-12 — Iterate and export:** 训练/部署/比较 v2，导出各产品资源；至少一个阶段由外部工具独立消费，证明闭环不是强绑定管线。
- [ ] **V1-GATE — Text Model Lifecycle V1 usable:** P0-GATE、P1-GATE、P2-GATE、P3-GATE、P4-GATE 和必选 X-*、V1-* 全部有完整 Actual PASS 证据；用户可以在目标环境完成一次成功迭代和一次有价值的反馈迭代。

最终质量标准不是“十个仓库 CI green”。它是用户能否在真实环境中完成上述旅程，并在失败、恢复、权限、usage、artifact 和数据反馈环节看到诚实且可复核的结果。

## 11. OSS adoption policy / 开源组件采用原则

Cyrene 拥有 Product semantics、authority、UX 和跨产品交接；成熟 OSS 作为可替换 engine、adapter 或 plugin：

| Component | V1 role / V1 角色 | Rule / 规则 |
| --- | --- | --- |
| DeepSeek Harness | Navigator upstream Agent Runtime/Session/Tool/Plugin/UI | 固定 pin；Profile/Bundle/plugin/UI extension 优先；上游 Core patch 目标为 0 |
| LLaMA Factory | Yield training engine | 复用现有 adapter 和 UI/engine；Yield 拥有 TrainingRun/ModelVersion；真实 CUDA 才算 acceptance |
| vLLM | Reactor serving engine | Reactor 拥有 Deployment/Endpoint；真实 model load/inference 才是 ready |
| DuckDB | Catalyst embedded data processor | 处理端口可替换；Catalyst 拥有 Dataset/DatasetVersion |
| Inspect AI | Echo optional evaluation adapter | 通过 EvaluationExecutionPort；Echo 拥有 EvaluationRun/Result/FeedbackSet |
| Cleanlab | Catalyst quality adapter/plugin | 只负责质量检测；不拥有 Dataset/Workspace/permissions |
| Distilabel | Optional Catalyst/Echo data/feedback adapter | 仅在真实需求和许可证/运行时证据充足时接入 |
| Argilla/Label Studio | UX reference or optional external import/export | 不把其 User/Workspace/Dataset/Backend 整体嵌入 Product Core |
| Exchange | Cyrene-owned gateway/routing | 继续由 Cyrene 拥有 Route/Auth/Usage；第一版不引入第二套路由真源 |

Navigator 的 TypeScript/Cordis 层只承担 Harness Service/Tool 注册、配置、生命周期、取消、事件和错误适配。已有 Rust 能力继续由受监管 binary/cyrene-native-host 承载。默认使用 inherited stdio 和 versioned IPC，不使用固定监听端口，不要求 Node Native Addon。

## 12. Deferred scope / 明确延后

以下内容不阻塞 Text Model Lifecycle V1 的第一条真实路径：

- 多模态模型、图像/音频/视频输入输出；
- AMD、TPU、其它 accelerator 和异构后端；
- 多节点联合训练、复杂分布式 optimizer、弹性训练和全局高级 scheduler；
- 全量 OpenAI API surface、所有 provider 特性和所有 tool schema；
- 自动化数据闭环、自动 gate 发布或未经用户选择的 feedback 回流；
- AutoML、无限制工作流 marketplace、把所有产品合成一个控制面板；
- 将 Argilla/Label Studio 等外部系统的完整用户、权限、数据库和 backend 嵌入 Catalyst；
- 为了新路线大规模迁移或重写已有 Rust 能力为 TypeScript；
- 在 adoption proof 之前继续扩张自研通用 Harness Core。

## 13. Execution order / 执行顺序

实现应按依赖顺序进行：

1. **Phase 0:** 固定 DeepSeek Harness、Profile/Bundle、Exchange tool path、Cyrene persistence、Rust stdio Tool、Codex import、Windows package 和 Core patch audit。
2. **Phase 1:** 先打通一张 NVIDIA GPU 上的现有文本模型消费路径，建立 Reactor/vLLM/Exchange/Navigator 的每日可见 vertical。
3. **Phase 2:** 再把 Catalyst DatasetVersion 接到 Yield 的真实 CUDA SFT LoRA、checkpoint/resume/export/ModelVersion，并回到 Reactor。
4. **Phase 3:** 把真实 Navigator 会话交给 Echo，用户选择 feedback 后生成 Catalyst DatasetVersion v2，完成第二轮比较。
5. **Phase 4:** 最后补齐两台以上 GPU 节点共享、Workspace enrollment、SSO/RBAC、usage/cost/audit、plugin governance、团队恢复和运维能力。
6. **Final:** 按 V1-* 旅程做完整演练；未通过的阶段保持未勾选并记录限制，不以文档或单仓库绿色替代真实证据。

跨阶段可以并行做局部 contract、fixture、UI 和测试，但不能绕过 Phase 0 的 Navigator adoption decision，也不能让一个产品偷偷成为另一个产品的 authority。

## 14. Open decisions / 待明确但不阻塞计划创建的问题

这些问题必须在对应阶段的第一个实现任务中落定并写入证据，不在当前文档中假装已经决定：

- Phase 0 的 Cyrene persistence backend 具体部署形态、数据库技术和事件 schema 版本；约束是单一可写 Session 真源、ordered append、flush、takeover 和审计。
- Windows Tauri shell 承载 upstream Web UI 的最终打包方式、嵌入运行时和签名流程；约束是干净机器可运行、无固定端口、无 Node Native Addon。
- 第一条 Phase 1 模型 fixture 和 GPU/VRAM profile；候选模型不能替代最终锁定的 revision、license、chat template 和资源证据。
- Exchange tool-call/usage 协议对 vLLM、外部 Provider 和 Navigator Harness 的最小交集；不为兼容所有 OpenAI API 而扩张 V1。
- ModelVersion 对 adapter-only、merged weights、quantized artifact、tokenizer 和 runtime compatibility 的 manifest 表达；训练成功不能自动意味着可部署。
- 多设备阶段的最小资源调度策略；V1 可先保证一张 GPU 一个作业、明确 queue/lease/failure，再扩展复杂调度。

## 15. Evidence ledger template / 证据台账模板

每个阶段的检查项都使用同一套台账字段。实施者可在对应章节下追加行或链接独立报告；下表只是模板，当前不代表任何 gate 已通过：

| Check ID | Commit / PR | Commands | Result | Environment | Limitations | Status |
| --- | --- | --- | --- | --- | --- | --- |
| <P0/P1/P2/P3/P4/X/V1-ID> | <immutable SHA or PR> | <exact command/config> | <counts, IDs, outcome> | <OS/CUDA/GPU/runtime/identity> | <gaps/blockers> | CODE_COMPLETE / LOCAL_TEST_PASS / ACTUAL_PASS / FAILED / BLOCKED |

阶段结束时必须把该阶段的所有检查 ID 映射到台账；没有对应行、链接或完整字段的 [x] 都应在 review 中退回。远程 CI 认证失败、外部服务不可用、Windows runner 不可用、GPU 不足等属于限制或阻塞，必须和源代码失败分开报告。

## 16. First implementation handoff / 首个实现交接

下一步实现从 Phase 0 开始，建议第一个可审查切片只包含：

1. 精确 DeepSeek Harness pin、lock 和 Profile/Bundle scaffold；
2. Exchange request/stream/tool/usage contract gap 的最小补齐；
3. Cyrene persistence backend 的事件/单写者/flush contract fixture；
4. 一个 inherited-stdio Rust Tool fixture；
5. P0 检查脚本、真实环境矩阵和证据目录约定。

该切片完成代码和局部测试后，仍需按照 P0-09..P0-24、P0-28..P0-32 做真实进程、真实 persistence、真实 Rust binary 和真实 Windows package 验收。只有 P0-GATE 通过，才把 Navigator 的长期实现方向正式切换为 DeepSeek Harness Profile/Bundle，并停止继续自研通用 Harness Runtime。
