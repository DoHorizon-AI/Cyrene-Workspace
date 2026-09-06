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
