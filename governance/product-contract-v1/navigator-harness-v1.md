# Navigator Harness authority / Navigator Harness 权威边界

Status: accepted V1 direction, implementation in progress. This is an evolution
of the Alpha snapshot reader contract, not a claim that a new implementation has
already reached canonical acceptance. The merged SHA registry remains historical
evidence for the Alpha baseline.

状态：用户已接受 V1 方向，正在实现。本文演进 Alpha 的只读快照客户端边界，不把
新实现冒充已合并验收；registry 中原有提交继续作为历史基线证据。

| Owner / 拥有者 | Authority / 权威 |
| --- | --- |
| DeepSeek Harness | Agent loop, Session event vocabulary, Tool lifecycle, generic approvals, Skill/Workflow and plugin runtime |
| Navigator | Conversation and AgentRun product semantics, client UX, import/export, sharing presentation and resource handoff |
| Cyrene persistence backend | One ordered append-only Session record, write lease/epoch, durable flush, restore and authorized read |
| Workspace / Identity | User, organization, membership, permission and identity policy |
| Exchange | Managed model routing, authentication and authoritative request Usage |
| Platform | Device, placement, execution, lease and Artifact authority |

Navigator can mutate its own Conversation/AgentRun resources. It remains a
consumer of DatasetVersion, TrainingRun, Deployment, Route and Platform
Operation; the former snapshot reader does not turn into a suite control plane.

Navigator 可以维护自己的 Conversation/AgentRun；对 DatasetVersion、TrainingRun、
Deployment、Route 和 Platform Operation 仍是资源消费者，不成为统一控制面板。

The upstream Session event model is persisted through Cyrene's implementation of
`SessionPersistence` / `SessionHandle`. The local JSONL persistence plugin is
disabled. Local projections and UI caches are rebuildable and cannot accept
independent message-history writes. Product metadata may be stored separately;
it cannot create another writable conversation transcript.

Harness Session events 是持久化数据本身；跨 Product 的 CloudEvents 是通知。
两者不可混淆：前者按单写者顺序追加到 Cyrene，后者仍提示消费者回读 owning API。
旧文档中 “Event != source of truth” 专指跨产品通知，不禁止本产品使用事件存储。

The adoption proof uses controlled workspace-scoped service credentials. This
does not establish OIDC, organizational RBAC or production sharing acceptance.
Those are Phase 4 requirements. A fenced writer must stop live work through the
upstream Agent cancellation API; reading history must never re-run a Tool.

Phase 0 的受控 Workspace credential 只用于证明持久化与执行边界；OIDC、组织级
RBAC 与企业共享仍需 Phase 4 验收。旧 writer 被 fencing 后，通过上游取消 API
停止活动执行；历史读取和导入不得再次执行 Tool。

Model requests in the adoption proof go through Exchange. A personal Navigator
profile may also connect directly to an external provider. That independent
entry does not change Session, Workspace or Platform authority.

验收模型请求经过 Exchange；个人 Navigator 也可直接连接外部 Provider。独立入口
不改变 Session、Workspace 和 Platform 的权威边界。

See the [V1 implementation checklist](../../docs/plans/cyrene-text-model-lifecycle-v1.md)
for gates, evidence, and the condition for retiring custom generic Harness work.
---
<!-- Chinese Translation / 中文翻译 -->

# Navigator Harness 权威边界

状态：已接受 V1 方向，正在实现。本文是在 Alpha 快照读取器合约基础上的演进，并不表示新实现已经通过规范验收。已合并的 SHA registry 仍作为 Alpha 基线的历史证据。

本文件记录 Navigator 的 V1 权威演进。下表保留 Alpha 的历史事实；新的 Harness 实现及验收单独跟踪，不能把基线快照读取能力当作最终产品定位。

| 所有者 | 权威职责 |
|---|---|
| DeepSeek Harness | Agent loop、Session 事件词汇、Tool 生命周期、通用审批、Skill/Workflow 和插件运行时 |
| Navigator | Conversation 和 AgentRun 产品语义、客户端 UX、导入 / 导出、共享呈现和资源交接 |
| Cyrene persistence backend | 单一有序追加式 Session 记录、write lease/epoch、持久化刷新、恢复和授权读取 |
| Workspace / Identity | 用户、组织、成员身份、权限和身份策略 |
| Exchange | 托管模型路由、身份验证和权威请求 Usage |
| Platform | 设备、放置、执行、租约和 Artifact 权威 |

Navigator 可以修改自己的 Conversation/AgentRun 资源。它仍然是 DatasetVersion、TrainingRun、Deployment、Route 和 Platform Operation 的消费者；原有快照读取器不会因此变成统一控制平面。

Navigator 可以维护自己的 Conversation/AgentRun；对 DatasetVersion、TrainingRun、Deployment、Route 和 Platform Operation 仍是资源消费者，不成为统一控制面板。

上游 Session 事件模型通过 Cyrene 对 `SessionPersistence` / `SessionHandle` 的实现持久化。本地 JSONL persistence 插件处于禁用状态。本地投影和 UI 缓存都可以重建，不允许接受独立的消息历史写入。Product 元数据可以单独保存，但不得创建另一份可写对话记录。

Harness Session 事件本身就是持久化数据；跨 Product 的 CloudEvents 是通知。二者不能混淆：前者按单写者顺序追加到 Cyrene；后者仍提示消费者从所属 API 回读。旧文档中的“Event != source of truth”专指跨产品通知，不禁止本 Product 使用事件存储。

采纳证明使用受控的 Workspace 作用域服务凭据。这不代表已经通过 OIDC、组织级 RBAC 或生产共享验收；这些属于 Phase 4 要求。被 fencing 的 writer 必须通过上游 Agent cancellation API 停止正在执行的工作；读取历史绝不能重新运行 Tool。

Phase 0 的受控 Workspace credential 只用于证明持久化与执行边界；OIDC、组织级 RBAC 与企业共享仍需 Phase 4 验收。旧 writer 被 fencing 后，通过上游取消 API 停止活动执行；历史读取和导入不得再次执行 Tool。

采纳证明中的模型请求会经过 Exchange。个人 Navigator 配置档也可以直接连接外部 Provider。此独立入口不会改变 Session、Workspace 或 Platform 的权威归属。

验收模型请求经过 Exchange；个人 Navigator 也可直接连接外部 Provider。独立入口不改变 Session、Workspace 和 Platform 的权威边界。

阶段门禁、证据以及退役自定义通用 Harness 工作的条件，请参阅 [V1 实施清单](../../docs/plans/cyrene-text-model-lifecycle-v1.md)。
