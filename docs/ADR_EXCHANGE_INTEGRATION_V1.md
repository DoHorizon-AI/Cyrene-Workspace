# ADR: Internal Exchange Integration v1

- **Status:** Architecture accepted; implementation and native acceptance are not yet verified.
- **Date:** 2026-10-12
- **Scope:** One internal organization, one GitHub repository, and one WeCom group for the first vertical.
- **Decision owners:** Platform owns identity, directory, policy, and delegation; Plugins Account Center owns external account links and credentials; Exchange owns integration business state and its REST facade; Navigator remains a consumer.

This ADR records the approved architecture for a deterministic GitHub-to-WeCom workflow. It is a contract and ownership decision, not evidence that the code, deployment, or provider acceptance has passed. Source changes in the Platform, Plugins, and Exchange task worktrees remain pending their owners' checks and normal integration.

本 ADR 记录已接受的 GitHub 到企业微信群确定性工作流架构。它用于冻结合约和权属，不代表代码、部署或真实服务商验收已通过。Platform、Plugins 和 Exchange 任务工作区中的源码改动仍须由各自 owner 验证并正常集成。

## Context and exact starting baselines

The task began from the following pinned integration branches. These SHAs are reproducibility anchors for this decision; they are not claims that subsequent work is merged or accepted.

| Repository | Task branch | Exact baseline SHA | Relevant existing surface at baseline |
|---|---|---|---|
| Cyrene-Workspace | `codex/integration-v1-workspace-20261012` | `c7087c5282c77cc3bfec0675ec49b991ab7235ec` | Product contract v1 governance, event and Problem Details schemas, async HTTP semantics |
| Cyrene-Platform | `codex/integration-v1-platform-20261012` | `587d35588e8590abab5bd645ba09bbc480479daa` | Workspace Authority Service v2, current directory and policy, authority outbox, signer and target resolution |
| Cyrene-Plugins-Official | `codex/integration-v1-plugins-20261012` | `c9b8d4b7d4923d42368de3b3ec2db87ee97c3fa7` | `DirectPluginRuntime`, `tool.provider.v1`, `message.connector.v1`, provider/connector capability and generated contract infrastructure |
| Cyrene-Exchange | `codex/integration-v1-exchange-20261012` | `0b0427d09a1559c02590c79345c72109b75f1610` | Product API, OpenAPI v1 and v2 catalog, gateway persistence, `model.provider.v1` adapter and product runtime modes |
| Cyrene-Navigator | `codex/integration-v1-navigator-20261012` | `33a153a252e03c362eb8b8472e401676fa5c0297` | Work persistence, task/approval/outbox contracts, ProductReadPort and Navigator Work API |

Current baseline reuse is deliberate: extend the Platform authority service instead of adding a second identity service; use the existing generic Plugin runtime and typed capability contracts; add an Exchange-owned integration module beside the gateway; and leave Navigator's work store and APIs unchanged. The gaps are delegated authority with current-policy revalidation, external account ownership and short credential leases, a typed Exchange inbox/outbox/operation lifecycle and API surface, and provider wire correctness plus end-to-end evidence. Exact implementation and test status belongs to each repository's normal review and CI evidence.

## Decision and ownership

The first vertical is fixed to one organization, one configured GitHub repository, and one configured WeCom group. Fixed issue events and explicit user commands follow typed rules through Exchange. The workflow must not call an LLM, Navigator's DSH, or a generic workflow engine. It must not change the existing model gateway behavior.

| Data or decision | Canonical owner | Stored data and boundary |
|---|---|---|
| Cyrene user, organization, workspace, policy and revocable delegation grant | Platform | Platform authority store; grant binds issuer and subject, organization/workspace, authenticated Exchange workload and audience, owner/operation allowlist, Product resource scopes, expiry (default seven days), generation and revocation state. No external account identity or credential is stored here. |
| External GitHub/WeCom account links, link sessions and credential lifecycle | Plugins Account Center | Private persistent account metadata and encrypted credential material; master-key reference is separately injected. Exchange stores only opaque Account/Link references and never copies credentials. Account Center's SQLite is private to that capability. |
| Integration connection/binding, inbox event, notification outbox job, operation, delivery receipt, resource snapshot and audit | Exchange | One separate SQLite WAL database for the first Exchange integration vertical. It stores opaque Account/Delegation references only, never credential or account-link payloads. It is not shared with Account Center. |
| Local Navigator tasks, approvals, schedules, memory, and execution | Navigator | Existing Navigator Work authority remains unchanged. Navigator is a later typed REST consumer only. |

This is an internal deployment profile, not a new cross-service database. It keeps the approved SQLite MVP while preserving one owner per concept. SQLite persistence is suitable for the bounded first vertical; high availability and multi-replica write coordination are outside this decision.

## Authority, account linking, and execution

Platform remains the identity and generic authorization authority. Extend the existing `WorkspaceAuthorityService` v2 with `CreateDelegation`, `RevokeDelegation`, and `ApproveAndEnqueueDelegatedInvocation`. A user explicitly grants a revocable seven-day default delegation for the Exchange workload; it carries an exact owner/operation allowlist and Product resource scopes. It permits matching user commands to run automatically without an approval for every message. It is never a blanket organization grant.

At grant creation, enqueue, claim/dispatch, and execution authorization, Platform rechecks the current Directory and policy. Authenticate Exchange from trusted managed mTLS or equivalent channel identity; never trust a request `workload_id`, actor, or roles field. Keep legacy interactive envelopes working. Delegated authorization is additive, explicitly versioned, and incompatible component profiles fail closed. Execution credentials last no more than 300 seconds; an expired pending invocation cannot authorize later execution. Revoke increments the grant generation and prevents stale queued work from dispatching.

Plugins Account Center is the only owner of external links and credentials. Anchor links to a verified `(issuer, subject, organization_id)` tuple, optionally scoped to a workspace. Provider identity includes its authority scope and stable subject. Never auto-merge on email or display name and never silently move a same-organization provider identity to a different Cyrene anchor. GitHub OAuth uses fixed official endpoints, one-time state/nonce bound to the primary actor, a configured callback URI, and API verification of the numeric GitHub user ID. GitHub writes execute as that linked human actor; installation or bot credentials are not a fallback. WeCom linking requires a challenge observed from an authenticated connector source and explicit confirmation by the primary user; CLI authentication status is not identity proof.

Because `DirectPluginRuntime` does not supply a standard user identity, Account Center user methods use a capability-specific signed caller context containing `caller_id`, issuer, subject, organization, workspace, request ID, issued/expiry times, operation ID, payload SHA-256, and HMAC-SHA256 signature over canonical context bound to the exact method and canonical request body. Keys are private per-caller secret references. Only configured Exchange can call user methods; only configured provider code can redeem a one-use credential lease. Callback and WeCom challenge methods use dedicated service contexts and recover the owner from a stored short-lived link session. This context proves a trusted Product-to-capability request; it does not create a new identity authority and has no roles field.

Credential leases are opaque, single-use, at most 60 seconds, and bound to actor, account, provider, operation, resource, and argument digest. Only the authenticated target provider can redeem a lease. Lease contents never appear in public REST or model tools. Provider token material stays in memory inside the authorized provider HTTP client. Unlink immediately invalidates leases; remote revocation status is recorded separately from local unlink.

## Durable workflow and provider contracts

Exchange owns `IntegrationPrincipal` (constructed only by its authorizer), `IntegrationConnection`, `SyncBinding`, `IncomingEvent`, `OutboxJob`, `Operation`, `DeliveryReceipt`, `ResourceSnapshot`, and `OperationAudit`. `IntegrationPrincipal` contains issuer, subject, organization/workspace, allowed operation IDs, and optional delegation ID/generation/expiry; API JSON cannot supply or override it. Exchange persistence uses scoped transactions, SQLite WAL, opaque stable IDs, organization/workspace checks, and restart-safe state.

Verified GitHub deliveries are normalized and atomically persisted to the inbox and notification outbox, deduplicated by connection plus delivery ID and payload fingerprint. A command uses the same durable operation for retries under the same scoped idempotency key and body hash; a key reused with a different body returns 409. Claim is compare-and-set with a fencing token. Persist `SENDING` before an external write. If a process crashes after sending may have started, or a claim expires after sending, mark the result `UNKNOWN`; never blindly retry a write. Read-only retries are bounded and respect provider `Retry-After`. Reconciliation is read-safe. An unknown write remains manual/reconcile-only until evidence resolves it.

The supported command grammar is limited to issue query, comment, close, and reopen. Ignore self-generated events and notification loops. No dispatcher path calls Agent, LLM, or model APIs. Test substitutes are allowed only in tests; missing production authorization, dependency resolution, or provider transport fails closed.

Plugins owns capability payloads and implementations: Account Center capability `account.center.v1`; GitHub Issues provider capability `tool.provider.v1` with exactly `github.issue.get`, `github.issue.comment.create`, and `github.issue.state.update` (`OPEN`/`CLOSED` only); and WeCom connector capability `message.connector.v1`. GitHub issue selection uses a configured stable repository identity and issue number, never arbitrary URL or shell execution. GitHub API errors map to fixed sanitized codes, and a write needs valid API result/receipt evidence.

The WeCom adapter must use official `aibot_send_msg` and `aibot_respond_msg` fields (`chatid`, `chattype`, `msgtype`, `msgid`, `from.userid`) and preserve the original callback `req_id` for replies. Validate explicit acknowledgement errors; a missing error code is not success. Do not substitute the request ID for a vendor message ID. `ACCEPTED` means only that the vendor accepted a request; the vendor message ID may be absent. Keep transport request ID separate and never infer delivery or read proof from acceptance. Disable direct Navigator forwarding for these bindings so the path remains deterministic and zero-LLM.

## REST, OpenAPI, catalog, and compatibility

Exchange adds the integration REST surface alongside existing gateway routes:

| Route family | Purpose |
|---|---|
| `/api/v1/accounts/link-sessions`, `/api/v1/accounts/links`, and credential authorization/unlink operations | Actor-scoped link management; no public credential lease redemption |
| `/api/v1/integrations/connections`, `/sync-bindings`, `/jobs`, and `/operations` | Scoped integration configuration, durable jobs, and operation readback |
| `/api/v1/integrations/bindings/{binding_id}/issues/...` | Fixed issue read and comment/state commands |
| `/api/v1/integrations/operations/{operation_id}/reconcile` | Read-safe reconciliation of unresolved work |
| `/api/v1/integrations/hooks/github/{connection_id}` | GitHub signature-verified webhook ingress |

The Exchange owner publishes typed OpenAPI and the v2 catalog together. Required stable operation IDs are `accountBeginLink`, `accountListLinks`, `accountUnlink`, `integrationListConnections`, `integrationCreateConnection`, `integrationListBindings`, `integrationCreateBinding`, `integrationGetIssue`, `integrationCreateIssueComment`, `integrationUpdateIssueState`, `integrationGetOperation`, and `integrationReconcile`. User routes require a Platform-verified principal and exact permissions. OAuth callback and provider webhook routes use native state/signature verification, never user-body identity. Mutations require `Idempotency-Key`; accepted asynchronous work returns 202 with a Product operation `Location`. Errors reuse canonical RFC 9457 Problem Details and trace ID semantics. The CLI calls this API and does not access SQLite directly.

Runtime modes are `gateway-only` (default), `integration-only`, and `combined`. Integration/provider startup failure must not take down gateway-only readiness or alter gateway routes. Existing model gateway APIs, operation IDs, and persistence remain backward-compatible. New REST paths and catalog entries are additive. New delegated authority is explicitly negotiated; older Platform profiles reject delegated invocations rather than silently treating them as user sessions. Database migrations retain a version marker and reject unknown newer schemas without destructive downgrade. On partial failures, preserve known durable state and surface `UNKNOWN` where an external write may have happened; never map uncertain delivery to success or safe retry.

## Staged delivery and acceptance evidence

The work is split into six reviewable PR stages. The ADR/authority documentation is the first scoped Workspace commit. Each implementation PR must carry its repository-specific validation and must not claim another owner's work as merged or accepted.

| Stage | Repository and scope | Required evidence before acceptance |
|---|---|---|
| 1. Architecture | Workspace ADR, index, scoped authority references | Markdown/link review; exact baselines and ownership recorded; no runtime acceptance claimed |
| 2. Delegation | Platform v2 additive RPCs, storage, current-policy checks, trusted workload auth | Existing v1/v2 compatibility tests; grant/revoke/expiry/generation, stale claim and cross-scope tests; generated bindings and native service checks |
| 3. Accounts and GitHub | Plugins Account Center, GitHub provider, typed contracts and credential leases | Identity-anchor, OAuth state/nonce, unlink/lease, provider ACL and sanitized error tests; official API behavior validated with controlled actor UAT |
| 4. WeCom transport | Plugins connector wire names, fields, callback correlation and ACK semantics | Official wire-contract tests for send/reply, rejected ACK, missing error code, empty vendor ID, and request correlation |
| 5. Exchange product | SQLite integration store/dispatcher, REST/OpenAPI/catalog, runtime modes and CLI API client | Restart, deduplication, idempotency conflict, crash-after-send `UNKNOWN`, bounded read retry, revoke-before-dispatch, zero-model-call spy, schema/catalog parity, gateway regression and native process checks |
| 6. Consumer and deployment | After REST contracts stabilize: narrow Navigator typed Exchange tools and deployment/acceptance docs | Client contract compatibility and end-to-end vertical using real provider test tenants; no Navigator Work store/API ownership changes |

Required end-to-end coverage includes signed GitHub webhook to persisted inbox, restart recovery, dispatcher to WeCom acknowledgement/unknown state, linked GitHub actor with actual provider ACL, Platform delegation/revocation, duplicate delivery, cross-organization rejection, nonce replay, and a zero-model-call spy. CI mocks prove protocol behavior only. Native Platform, Account Center, Exchange and WeCom deployment acceptance, authorized GitHub actor UAT, and real tenant end-to-end results are **NOT_RUN** until the relevant environment and evidence exist. No source implementation, hosted CI, native service, or provider acceptance is asserted by this ADR.

## Accepted contract references

- Platform v2 authority source of truth: `Cyrene-Platform/contracts/proto/cyrene/workspace/authority/v2/workspace_authority.proto`, plus its mirror at `Cyrene-Platform/contracts/rust/cy-proto/proto/cyrene/workspace/authority/v2/workspace_authority.proto`.
- Existing Product HTTP semantics and errors: [Product HTTP semantics](../governance/product-contract-v1/product-http-semantics.md), [Problem Details schema](../governance/product-contract-v1/problem-details.schema.json), and [Product event schema](../governance/product-contract-v1/product-event.schema.json).
- Plugin capability ownership: `Cyrene-Plugins-Official/contracts/capabilities.yaml`; `contracts/proto/cyrene/tool/provider/v1/tool_provider.proto`; `contracts/proto/cyrene/message/connector/v1/message_connector.proto`; Account Center's `plugins/accounts/account-center/contracts/v1/` schemas.
- Exchange's existing public contract and catalog locations: `Cyrene-Exchange/contracts/product/v1/openapi.yaml` and `Cyrene-Exchange/contracts/product/v2/catalog.json`. The new integration routes and operation IDs belong in these owner-maintained artifacts after implementation stabilizes.
- Navigator Work authority reference: `Cyrene-Navigator/contracts/product/v1/work.openapi.json`. This ADR adds no Navigator Work route or durable entity.

These cross-repository paths identify owners' canonical artifacts, not copies to be authored in Workspace. Workspace remains the architecture and product-contract governance repository.

---
<!-- Chinese Translation / 中文翻译 -->

# ADR：Exchange 内部集成 V1

- **状态：** 架构已接受；实现和原生验收尚未验证。
- **日期：** 2026-10-12
- **范围：** 首个纵向流程限定为一个内部组织、一个 GitHub 仓库和一个企业微信群。
- **决策权属：** Platform 负责身份、目录、策略和委托；Plugins Account Center 负责外部账号关联和凭据；Exchange 负责集成业务状态与 REST 门面；Navigator 保持消费者定位。

本 ADR 记录已批准的确定性 GitHub 到企业微信群工作流架构。它是合约与权属决策，不是代码、部署或服务商验收通过的证据。Platform、Plugins 和 Exchange 任务工作区中的源码改动仍待各仓库 owner 检查、验证并正常集成。

## 背景与精确起始基线

任务从以下固定集成分支基线开始。这些 SHA 用于复现决策时的起点，不表示后续工作已合并或验收。

| 仓库 | 任务分支 | 精确基线 SHA | 基线中已有的相关表面 |
|---|---|---|---|
| Cyrene-Workspace | `codex/integration-v1-workspace-20261012` | `c7087c5282c77cc3bfec0675ec49b991ab7235ec` | Product contract v1 治理、事件和 Problem Details schema、异步 HTTP 语义 |
| Cyrene-Platform | `codex/integration-v1-platform-20261012` | `587d35588e8590abab5bd645ba09bbc480479daa` | Workspace Authority Service v2、现有目录与策略、authority outbox、签名器和目标解析 |
| Cyrene-Plugins-Official | `codex/integration-v1-plugins-20261012` | `c9b8d4b7d4923d42368de3b3ec2db87ee97c3fa7` | `DirectPluginRuntime`、`tool.provider.v1`、`message.connector.v1`、Provider/Connector 能力和生成合约基础设施 |
| Cyrene-Exchange | `codex/integration-v1-exchange-20261012` | `0b0427d09a1559c02590c79345c72109b75f1610` | Product API、OpenAPI v1 与 v2 catalog、网关持久化、`model.provider.v1` 适配器和产品运行模式 |
| Cyrene-Navigator | `codex/integration-v1-navigator-20261012` | `33a153a252e03c362eb8b8472e401676fa5c0297` | Work 持久化、任务/审批/outbox 合约、ProductReadPort 和 Navigator Work API |

本决策有意复用现有边界：扩展 Platform 权限服务，而非另建身份服务；采用既有通用 Plugin runtime 和有类型的 capability 合约；在 Exchange 网关旁增加 Exchange 自有集成模块；保持 Navigator 的 work store 和 API 不变。待补齐的部分包括：带当前策略复核的委托权限、外部账号归属与短时凭据租约、Exchange 自有类型化 inbox/outbox/operation 生命周期及 API，以及正确的 Provider wire 行为和端到端证据。实现与测试的精确状态以各仓库正常评审和 CI 证据为准。

## 决策与权属

首个纵向流程固定为一个组织、一个已配置 GitHub 仓库和一个已配置企业微信群。固定 issue 事件和明确用户命令经由 Exchange 中的有类型规则执行。工作流不得调用 LLM、Navigator 的 DSH 或通用工作流引擎，也不得改变现有 model gateway 行为。

| 数据或决策 | 规范权属 | 存储数据和边界 |
|---|---|---|
| Cyrene 用户、组织、工作区、策略和可撤销委托 | Platform | Platform authority store；委托绑定 issuer/subject、组织/工作区、经过认证的 Exchange workload 与 audience、owner/operation 白名单、Product 资源范围、到期时间（默认七天）、generation 和撤销状态。此处不存外部账号身份或凭据。 |
| 外部 GitHub/WeCom 账号关联、link session 和凭据生命周期 | Plugins Account Center | 私有持久账号元数据与加密凭据材料；master-key 引用单独注入。Exchange 只存不透明 Account/Link 引用，不复制凭据。Account Center 的 SQLite 仅供该 capability 私有使用。 |
| 集成 connection/binding、inbox event、通知 outbox job、operation、delivery receipt、资源快照和审计 | Exchange | Exchange 首个集成纵向流程使用独立的 SQLite WAL 数据库。仅保存不透明 Account/Delegation 引用，不保存凭据或账号关联数据。它不与 Account Center 共用数据库。 |
| Navigator 本地任务、审批、日程、记忆和执行 | Navigator | 现有 Navigator Work 权威保持不变；Navigator 后续仅作为类型化 REST 消费者。 |

这是内部部署配置，不是跨服务共享数据库。它保留已批准的 SQLite MVP，同时确保每个概念只有一个 owner。SQLite 适用于边界明确的首个纵向流程；高可用和多副本写协调不在本决策范围内。

## 权限、账号关联与执行

Platform 继续作为身份和通用授权权威。向现有 `WorkspaceAuthorityService` v2 增加 `CreateDelegation`、`RevokeDelegation` 和 `ApproveAndEnqueueDelegatedInvocation`。用户显式授予 Exchange workload 一个默认七天、可撤销的委托，包含精确的 owner/operation 白名单和 Product 资源范围。范围匹配时，用户命令可自动执行，无需逐条消息审批；这不是组织级的宽泛授权。

Platform 在创建委托、入队、领取/派发和执行授权时重新检查当前 Directory 与策略。通过受信任的托管 mTLS 或等价通道身份认证 Exchange；不得相信请求里的 `workload_id`、actor 或 roles 字段。保留旧交互式 envelope 的兼容性。委托授权以显式版本追加，组件 profile 不兼容时必须 fail closed。执行凭据最长 300 秒；已过期的待执行 invocation 不得继续获得授权。撤销时递增 grant generation，阻止旧队列中的工作继续派发。

Plugins Account Center 是外部账号关联和凭据的唯一权威。账号关联锚定到已验证的 `(issuer, subject, organization_id)`，并可选工作区范围。Provider 身份包含 authority scope 和稳定 subject。禁止按邮箱或显示名自动合并，也禁止同一组织内的相同 Provider 身份静默迁移到不同 Cyrene 锚点。GitHub OAuth 使用固定官方端点、绑定主 actor 的一次性 state/nonce、配置好的 callback URI，并通过 API 验证 GitHub 数字用户 ID。GitHub 写操作以该 actor 关联的用户身份执行；不得退回 installation 或 bot 凭据。WeCom 关联必须来自已认证 connector source 的 challenge，并由主用户明确确认；CLI 登录状态不是身份凭证。

由于 `DirectPluginRuntime` 不提供标准用户身份，Account Center 用户方法采用 capability 专用的已签名 caller context，其中包含 `caller_id`、issuer、subject、organization、workspace、request ID、签发/过期时间、operation ID、payload SHA-256，以及 HMAC-SHA256 签名。签名使用规范化 context，并绑定精确方法和规范化请求体。密钥通过私有的 caller secret 引用提供。只有已配置的 Exchange 可以调用用户方法；只有已配置的 Provider 代码可以兑换一次性凭据租约。Callback 与 WeCom challenge 方法使用专用 service context，并从已存储的短时 link session 找回 owner。该 context 证明受信任的 Product 到 capability 请求，不创建新的身份权威，也不包含 roles 字段。

凭据租约是不透明、单次使用、最长 60 秒，并绑定 actor、account、provider、operation、resource 和 argument digest。只有经过认证的目标 Provider 能兑换租约。租约内容不得出现在公开 REST 或模型工具中。Provider token 仅保留在获准 Provider HTTP client 的内存中。解除关联会立即使租约失效；远端撤销状态与本地解除关联分别记录。

## 持久化工作流与 Provider 合约

Exchange 负责 `IntegrationPrincipal`（只能由 Exchange authorizer 构造）、`IntegrationConnection`、`SyncBinding`、`IncomingEvent`、`OutboxJob`、`Operation`、`DeliveryReceipt`、`ResourceSnapshot` 和 `OperationAudit`。`IntegrationPrincipal` 包含 issuer、subject、organization/workspace、允许的 operation ID，以及可选 delegation ID/generation/expiry；API JSON 不能提供或覆盖这些字段。Exchange 持久化使用按范围隔离的事务、SQLite WAL、不透明稳定 ID、organization/workspace 检查以及可跨重启恢复的状态。

已验证的 GitHub 投递会被规范化，并在同一事务中写入 inbox 和通知 outbox；按 connection、delivery ID 和 payload fingerprint 去重。命令使用相同范围内的幂等键与 body hash 重试时返回同一个持久 operation；相同 key 搭配不同 body 返回 409。领取采用 compare-and-set 和 fencing token。外部写入前先持久化 `SENDING`。如果进程在写操作可能开始后崩溃，或发送后 claim 过期，应将结果记为 `UNKNOWN`，不得盲目重试写入。只读重试次数有界并遵守 Provider 的 `Retry-After`。对账只进行只读检查。证据未解决前，未知写入只能人工处理或对账。

支持的命令语法仅限 issue 查询、评论、关闭和重新打开。忽略自生成事件和通知循环。Dispatcher 不调用 Agent、LLM 或模型 API。测试替身仅允许用于测试；生产授权、依赖解析或 Provider transport 缺失时必须 fail closed。

Plugins 负责 capability payload 和实现：Account Center 的 `account.center.v1`；GitHub Issues Provider 的 `tool.provider.v1`，且仅开放 `github.issue.get`、`github.issue.comment.create` 和 `github.issue.state.update`（状态只允许 `OPEN`/`CLOSED`）；以及 WeCom Connector 的 `message.connector.v1`。GitHub issue 选择使用配置的稳定仓库身份和 issue 编号，不能传任意 URL 或执行 shell。GitHub API 错误映射到固定的脱敏代码；写操作需要有效 API 结果/回执证据。

WeCom 适配器必须使用官方 `aibot_send_msg` 和 `aibot_respond_msg` 字段（`chatid`、`chattype`、`msgtype`、`msgid`、`from.userid`），并在回复中保留原始 callback `req_id`。必须验证显式 ACK 错误；缺少错误码不代表成功。不得用 request ID 冒充 vendor message ID。`ACCEPTED` 只表示 Provider 接受请求，vendor message ID 可以为空。transport request ID 单独保存，不能从 ACCEPTED 推断送达或已读。绑定应禁用直接转发到 Navigator，确保该路径保持确定性且零 LLM。

## REST、OpenAPI、catalog 与兼容性

Exchange 在现有 gateway route 旁增加集成 REST 表面：

| 路由族 | 用途 |
|---|---|
| `/api/v1/accounts/link-sessions`、`/api/v1/accounts/links` 和凭据授权/解除关联操作 | 按 actor 范围管理关联；不公开凭据租约兑换 |
| `/api/v1/integrations/connections`、`sync-bindings`、`jobs` 和 `operations` | 范围受限的集成配置、持久 job 和 operation 查询 |
| `/api/v1/integrations/bindings/{binding_id}/issues/...` | 固定 issue 读取与评论/状态命令 |
| `/api/v1/integrations/operations/{operation_id}/reconcile` | 对未决工作执行只读对账 |
| `/api/v1/integrations/hooks/github/{connection_id}` | 验证 GitHub 签名的 webhook 入口 |

Exchange owner 同步维护类型化 OpenAPI 与 v2 catalog。稳定 operation ID 包括 `accountBeginLink`、`accountListLinks`、`accountUnlink`、`integrationListConnections`、`integrationCreateConnection`、`integrationListBindings`、`integrationCreateBinding`、`integrationGetIssue`、`integrationCreateIssueComment`、`integrationUpdateIssueState`、`integrationGetOperation` 和 `integrationReconcile`。用户路由要求 Platform 验证过的 principal 和精确权限。OAuth callback 与 Provider webhook 路由使用原生 state/签名校验，不读取用户请求体中的身份。变更要求 `Idempotency-Key`；异步受理返回 202 和 Product operation `Location`。错误复用规范 RFC 9457 Problem Details 与 trace ID 语义。CLI 只能调用该 API，不能直接访问 SQLite。

运行模式为 `gateway-only`（默认）、`integration-only` 和 `combined`。集成或 Provider 启动失败不得影响 gateway-only readiness 或修改 gateway route。现有 model gateway API、operation ID 和持久化必须向后兼容。新增 REST path 与 catalog entry 采用追加方式。新增委托授权必须显式协商；较旧 Platform profile 拒绝 delegated invocation，不得把它静默当作用户会话。数据库迁移保留版本标记，遇到未知的更新 schema 时拒绝破坏性降级。部分失败时保留已知持久状态；外部写入结果不确定时报告 `UNKNOWN`，不得映射成成功或安全重试。

## 分阶段交付与验收证据

工作拆为六个可审查的 PR 阶段。Workspace ADR/权属文档应先作为首个 scoped commit。每个实现 PR 必须附上本仓库的验证结果，不得把其他 owner 的工作声明为已合并或已验收。

| 阶段 | 仓库与范围 | 接受前所需证据 |
|---|---|---|
| 1. 架构 | Workspace ADR、索引、范围内权属引用 | Markdown/link 审查；记录精确基线和权属；不声称运行时验收 |
| 2. 委托 | Platform v2 追加 RPC、存储、当前策略校验、可信 workload 认证 | 现有 v1/v2 兼容测试；grant/revoke/expiry/generation、过期 claim 和跨范围测试；生成 bindings 与原生服务检查 |
| 3. 账号与 GitHub | Plugins Account Center、GitHub Provider、有类型合约和凭据租约 | 身份锚点、OAuth state/nonce、解除关联/租约、Provider ACL 与脱敏错误测试；使用受控 actor UAT 验证官方 API 行为 |
| 4. WeCom transport | Plugins connector wire 名称、字段、callback 关联和 ACK 语义 | 官方 wire 合约测试覆盖发送/回复、拒绝 ACK、缺失错误码、空 vendor ID 和 request 关联 |
| 5. Exchange 产品 | SQLite 集成 store/dispatcher、REST/OpenAPI/catalog、运行模式和 CLI API client | 重启、去重、幂等冲突、发送后崩溃 `UNKNOWN`、有界只读重试、派发前撤销、零模型调用 spy、schema/catalog 一致性、gateway 回归和原生进程检查 |
| 6. 消费者与部署 | REST 合约稳定后：Navigator 精简的类型化 Exchange 工具及部署/验收文档 | 客户端合约兼容性和使用真实 Provider 测试租户的端到端纵向验证；不得改变 Navigator Work store/API 权属 |

端到端覆盖必须包括：签名 GitHub webhook 写入持久 inbox、重启恢复、dispatcher 到 WeCom ACK/未知状态、关联 actor 与真实 Provider ACL、Platform 委托/撤销、重复投递、跨组织拒绝、nonce 重放和零模型调用 spy。CI mock 只能证明协议行为。Platform、Account Center、Exchange、WeCom 的原生部署验收、授权 GitHub actor UAT 和真实租户端到端结果，只有在环境和证据存在后才能验证；当前均为 **NOT_RUN**。本 ADR 不声称源码实现、托管 CI、原生服务或 Provider 验收已通过。

## 已接受的合约引用

- Platform v2 authority 源文件：`Cyrene-Platform/contracts/proto/cyrene/workspace/authority/v2/workspace_authority.proto`，以及镜像 `Cyrene-Platform/contracts/rust/cy-proto/proto/cyrene/workspace/authority/v2/workspace_authority.proto`。
- 现有 Product HTTP 语义与错误格式：[Product HTTP semantics](../governance/product-contract-v1/product-http-semantics.md)、[Problem Details schema](../governance/product-contract-v1/problem-details.schema.json) 和 [Product event schema](../governance/product-contract-v1/product-event.schema.json)。
- Plugin capability 权属：`Cyrene-Plugins-Official/contracts/capabilities.yaml`、`contracts/proto/cyrene/tool/provider/v1/tool_provider.proto`、`contracts/proto/cyrene/message/connector/v1/message_connector.proto` 和 Account Center 的 `plugins/accounts/account-center/contracts/v1/` schemas。
- Exchange 现有公开合约和 catalog 位置：`Cyrene-Exchange/contracts/product/v1/openapi.yaml` 与 `Cyrene-Exchange/contracts/product/v2/catalog.json`。新集成 route 和 operation ID 稳定后，由 Exchange owner 写入这些权属制品。
- Navigator Work 权威引用：`Cyrene-Navigator/contracts/product/v1/work.openapi.json`。本 ADR 不增加 Navigator Work route 或持久实体。

这些跨仓库路径指向 owner 的规范制品，不是要求在 Workspace 复制合约。Workspace 仍是架构和 Product contract 治理仓库。
