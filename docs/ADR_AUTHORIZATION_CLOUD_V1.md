# ADR: Authorization, Cloud Observation, and Recovery v1

- **Status:** Design and local implementation evidence recorded; cross-repository integration and deployment acceptance are pending.
- **Date:** 2026-10-14
- **Scope:** Provider authorization health and user-led renewal, configured cloud-resource observations, durable notifications, and safe Navigator continuation.
- **Decision owners:** Platform owns Directory, policy, catalog grants, and generic resource authority; Plugins owns provider capabilities and native credentials; Exchange owns Product connections, observations, renewal jobs, and notifications; Navigator owns task continuation; Client owns presentation and explicit browser notification permission.

This ADR records the shared v1 boundary and the exact implementation snapshots reviewed for this task. It does not claim that the feature branches are merged, that the deployment is wired, or that a real tenant or provider operation passed. Each source SHA below identifies evidence only for that snapshot; newer branch commits and dirty worktrees require their own checks.

本 ADR 记录 v1 共享边界及本任务审核过的精确实现快照，不表示功能分支已合并、部署已接通，或真实租户/服务商操作已通过。下文每个源码 SHA 仅代表对应快照的证据；更新的分支提交和脏工作树须重新检查。

## Decision and authority boundaries

Use existing Product, Platform, and Plugin owners. This feature adds no duplicate identity service, cloud inventory authority, account-link database, or model-driven monitor.

| Concern | Owner and authoritative value | Boundary |
|---|---|---|
| User, organization, workspace, Directory role, operation grant, and generic resource/Lease/Fence authority | Platform | Resolve current policy and exact catalog operation grants for every request. A provider observation does not create a Platform resource or grant. |
| External identity association | Plugins Account Center | `accountLinkRef` identifies an Account Center-owned link and verified external actor. |
| Native provider profile or grant | Provider binding / native provider | `nativeProviderRef` (surfaced as a scoped `authorizationRef` where needed) identifies provider-owned authorization state. It is not an `accountLinkRef`, does not prove a linked external actor, and is not a Client or caller-provided credential. |
| Product connection, resource observation, monitor state, renewal intent, notification sequence, and send receipt | Exchange | Keep durable Product state in the existing integration store and return only Product-owned references. Resolve connection and provider binding from trusted server configuration. |
| Provider capability and native credentials | Plugins | Expose fixed, typed operations. Keep CLI profiles, OAuth secrets, SMTP credentials, and provider responses inside their owning adapter/configuration boundary. |
| Task pause and continuation | Navigator | Preserve the existing task and progress; do not create another task database or rerun the previous model turn. |
| User presentation | Client | Render Product-owned, actor-scoped data. Client state is not an authorization or resource authority. |

Cloud provider IDs and metadata remain provider observations. Exchange may attach a `platformResourceRef` or generation only when it receives that reference from Platform; it must not copy, derive, or register a cloud native ID as a Platform resource. Cloud API read capability does not imply Platform inventory, scheduling, placement, or lease authority.

## Product and Plugin contracts

The Plugin transport remains `tool.provider.v1`; the integration-support DTOs and signed caller context are shared contract types, not another runtime protocol. Product HTTP uses camelCase while Plugin JSON uses snake_case. The caller signs the exact inner `arguments` object; the provider verifies method/tool ID, operation, caller workload, audience, binding, issuer/subject, organization/workspace, request ID, payload hash, and short expiry before reading provider state or creating an intent. Verification keys come from binding-owned host configuration.

The fixed Plugin methods are:

| Tool ID | Fixed operation | Inner arguments |
|---|---|---|
| `wecom_authorization_health` | `wecom.authorization.health.read` | `{authorization_ref}` |
| `wecom_reauthorization_start` | `wecom.authorization.renewal.start` | `{authorization_ref}` |
| `wecom_reauthorization_status` | `wecom.authorization.renewal.status` | `{session_ref}` |
| `cloud_resources_query` | `cloud.resources.query` | `{target_ref}` |
| `cloud_authorization_health` | `cloud.authorization.health.read` | `{target_ref}` |
| `email_notification_send` | `email.notification.send` | `{recipient_ref, subject, text, request_ref}` |
| `email_connection_health` | `email.connection.health.read` | `{}` |

All calls use the closed `{caller_context, arguments}` envelope. Unknown fields, operation IDs, provider targets, recipient aliases, executable paths, profiles, URLs, or credentials from the caller are rejected. Product routes are separate, explicitly allowlisted translations; Product operation IDs are never forwarded as Plugin operation IDs.

The Product route seam is:

| Route | Semantics |
|---|---|
| `GET /api/v1/integrations/connections/{id}/authorization-health` | Current scoped authorization observation. |
| `POST /api/v1/accounts/authorizations/{id}/renewal-sessions` | Actor-authorized, durable renewal start; asynchronous work returns `202` and a Product `Location`. |
| `GET /api/v1/accounts/authorizations/{id}/renewal-sessions/{sessionId}` | Actor-owned status lookup, including recovery by the same signed start request identity when the provider supports it. |
| `GET /api/v1/integrations/connections/{id}/resources` | Read the latest configured cloud observations and their freshness. |
| `POST /api/v1/integrations/connections/{id}/actions/refresh` | Start a durable, read-only cloud refresh job. |
| `GET /api/v1/integrations/notifications?after={sequence}` | Read the durable actor-scoped notification feed. |
| `GET /api/v1/integrations/notifications/events` | Resume the feed with SSE and `Last-Event-ID`; recover gaps from the durable list. |
| `POST /api/v1/integrations/notifications/{id}/ack` | Acknowledge with the Product `Idempotency-Key` contract. |

Every API call uses the existing verified bearer principal, organization/workspace scope, exact operation permission, and current Platform catalog grant. Host configuration pins the active Exchange Product workload profile and operation catalog; a UI control or provider `authorizationRef` cannot grant access. Missing current identity, role, grant, binding, or host profile fails closed. Platform `AWAITING_AUTHORITY` remains distinct from Product provider reauthorization.

### HTTP error semantics

Only a confirmed provider grant that requires user reauthorization maps to HTTP `409` with `EXCHANGE_CONNECTION_REAUTHORIZATION_REQUIRED`. Its optional RFC 9457 `context` is a closed object containing only the verified `connectionId` and scoped `authorizationRef`; these references locate the already-authorized Product/provider binding and do not prove identity, role, credential possession, or a new grant. The schema rejects identity claims, roles, credentials, paths, and arbitrary extensions.

HTTP `401` retains caller-authentication semantics. Generic `403` remains an authorization denial, including a provider permission denial that does not establish a supported reauthorization flow. Network failures, unsupported protocol results, and unknown expiry stay unknown under the existing endpoint semantics; they do not become an expired/valid claim or a reauthorization `409`. Platform `AWAITING_AUTHORITY` is not a provider-auth failure.

## Runtime behavior and functional limits

| Feature | Required behavior | Explicit limit |
|---|---|---|
| Authorization health | `PRESENT` means only that the binding-owned local CLI profile is authorized. `VALID` requires a successful fixed read-only probe for the named capability. Distinguish confirmed permission denial, confirmed expiry, missing login, unavailable CLI, and unknown network/protocol outcomes. Report unknown expiry as `null`. | WeCom currently proves only `wecom.message.aibot.sessions.list`; it does not claim every WeCom permission is valid. A cloud binding reference is not a renewable grant. |
| WeCom renewal | Existing authorized profiles use only a sanitized official grant page returned by a recognized provider response. Do not run `auth init` to refresh an existing profile. Fixed noninteractive/no-browser CLI activation is reserved for an explicit initial onboarding when the profile is missing. Bind the CLI Bot identity, persist a private owner-scoped intent before process/UI side effects, and increment generation only after a fresh capability probe and identity match. | Unsupported or unsafe provider URLs return a scoped actionable error while unrelated tools and jobs continue. Profile/Bot identity changes require explicit relink; they cannot become `SUCCEEDED`. The provider's supported QR page is `https://work.weixin.qq.com/ai/qc/gen` with its fixed source marker and opaque code; redirects and arbitrary URLs are not accepted. |
| Cloud observations and monitoring | Poll fixed configured target references on a deterministic schedule (15-minute default); preserve the last observation on provider failure and include source freshness. Notify known expiry at 24 hours and 1 hour, confirmed invalidation immediately, and repeat an actionable unacknowledged incident at 6 hours; deduplicate by state/generation/stage and stay quiet on no change. The monitor and comparison path make zero LLM/DSH calls. | No provider-native ID is a Platform resource. An unknown provider result is not `STOPPED`, `VALID`, or expired. This is read-only inventory, not a cloud control plane. |
| Email notifications | Use the independent SMTP connector with host-owned endpoint/credential references and configured recipient aliases. Persist `SENDING` before the send and expose `ACCEPTED`, `UNKNOWN`, or `REJECTED` without claiming final delivery absent evidence. | Email does not depend on an open Client page. An accepted SMTP transaction is not proof that a recipient read or received the message. |
| Browser notifications | The Client may request the browser Notification permission only from an explicit user action. Keep the durable in-app feed as the recovery source. | Browser system notifications are not Web Push and do not reach a closed browser. No push subscription, background delivery, or closed-browser claim is included. |
| Navigator continuation | Exact provider `409` pauses dependent work as `AWAITING_USER_AUTHORIZATION`. After actor/workspace revalidation and a newer authorization generation, resume only unexecuted, allowlisted read-only work; preserve the existing task and progress. Independent tasks continue. | Do not replay the prior model turn. Unknown or possibly completed writes require reconciliation/manual resolution and are never automatically replayed. |

## Durable recovery rules

| Failure or transition | Recovery rule |
|---|---|
| Renewal start response is lost or ambiguous | Exchange persists an intent before the provider call. Retain the original scoped `Idempotency-Key`/signed request ID. A status lookup by the request ID is read-only and may recover the same session; do not start a second CLI authorization. If reconciliation remains ambiguous, leave it `UNKNOWN`/manual. |
| Same renewal key is repeated | Exact same scope and payload returns the original session. Same scope with a changed payload is rejected. Session status is owner-checked against binding, actor, organization, and workspace. |
| Provider reports a changed profile identity | Fail the renewal and require an explicit relink. Never transfer the prior binding's authorization or mark a different Bot as success. |
| Provider health is unknown | Preserve the prior observation and report `UNKNOWN`; do not guess expiry, raise the 409 gate, clear a grant, or auto-run initialization. |
| SMTP outcome may have followed DATA | Persist `UNKNOWN`; reconcile without another SMTP transaction. A known pre-send rejection may be handled under the exact provider retry policy. |
| Navigator/Exchange restarts during an authorized wait | Keep the existing task paused. Re-read the same actor-owned authorization and generation before any safe continuation; reconcile existing writes before proceeding. No original-turn or mutation replay. |
| Client closes or loses its live feed | On reopen, recover from the durable sequence endpoint. Browser notifications are best-effort only while browser APIs are available; independent configured email is the separate closed-browser channel. |

## Deployment and acceptance matrix

No stage below is considered production-deployed until normal owner PR merges, post-merge remote read-back, Platform policy/catalog packaging, and the required host/tenant checks are complete.

| Layer | Host configuration and deployment boundary | Evidence snapshot | Current gate |
|---|---|---|---|
| Shared Plugin contracts | `cyrene_integration_contracts` v0.1.0, signed-context verifier, schemas, and `tool.provider.v1` TCK. | Shared TCK passed 10 tests at `9f3ef7f617aaad0b1cf66d25056ae258de45b6a9`. A later contracts worktree tip is `f623facb012532b9d936dcd3fb4f6c269371eb6c` and has uncommitted TCK/SDK/packager edits; those edits are not covered by that archived validation. | Local contract evidence only; release packaging and current dirty changes need owner verification. |
| WeCom provider | Binding-owned isolated CLI profile, 32-byte caller-context key from host configuration, fixed CLI executable/profile, and private binding-scoped renewal store. Do not copy CLI credentials into Exchange. | `c7234c26a400c1d209d9938c3412da1def459508`; cross-review archive reports 87 offline tests, and the owner reran the current committed worktree with 90 tests and a pinned Navigator fixture. | Simulated only. Real `wecom-cli`, auth-init, tenant grant, and provider calls are `NOT_RUN`. |
| Cloud provider | Fixed provider/target bindings, provider-owned secret references, bounded HTTPS reads, and no caller-selected URL/account. | `3ff7c821dbb8c1cd50ad93c5f2d8a24a2eed23fe`; 34 fixture tests, lint/format, lock check, and local package build passed. | Simulated only; Azure/GCP/HF/Runpod/Tailscale calls are `NOT_RUN`. |
| SMTP provider | Independent connector, host-owned SMTP configuration and secret references, recipient alias allowlist. | `c53bf6322e7a74d579f1dc785e65e248e3a6b3a1`; 24 loopback SMTP protocol tests and local package checks passed. | Real SMTP send and delivery are `NOT_RUN`. |
| Exchange Product API/worker | Separate integration store and workers; use existing bearer principal and Platform-authorized catalog operation on every call. Configure the current host Product workload profile, provider binding refs, email profile, and pinned operation catalog outside request JSON. Gateway-only readiness remains independent. | `caa92a8c426212c61c253853614073881ae63c86`; 36 focused component tests passed. The exact snapshot is not production-wired; a later worktree contains uncommitted follow-up code not covered by that validation. | `NOT_READY` for production builder/current-context/profile/provider/email wiring and host-config exercise. |
| Platform policy and Workspace bundle | Platform host configuration must pin the Exchange workload profile, Product catalog snapshot, exact operation grants, Directory roles, owner bindings, workspace, and release lock. Missing entries remain default-deny. | Platform `origin/develop` snapshot `b294fe9e360c758e516c0b7d2c0a2291d0e1f299`; source review found the new operation IDs absent from current policy/catalog lock. | Feature grants and staged signed bundle are pending; source review is not grant acceptance. |
| Navigator | Existing Work API, task persistence, ownership checks, pause state, read-only child guard, and generation revalidation. | `a49c558e7d8e55b988e48993a5ba46ee28497d78`; 18 Python and 34 Node tests plus TypeScript compile passed. The Node run was v22.23.2 while the source lock specifies v24.13.0. | Simulated consumer tests; live DSH, Exchange authority, OAuth, and provider runtime are `NOT_RUN`. |
| Client | Existing paired Control session; expose WeCom renewal only when supported; use Product feed; request browser notification permission from explicit UI action; restrict links to the WeCom fixed origin/path/query. | Review tested `a5189d84ab012220729a63d4bf4cc28f96f8a02b` (11 focused tests; Node 22 build smoke). Current worker branch tip `031be23683add9f2d6a0073199613ff30d1684dd` adds same-key recovery, provider gating, fixed URL validation, and nullable feed IDs; the archived review predates that tip. | Latest Client code needs exact-head validation; browser permission, paired live session, and end-to-end UI are `NOT_RUN`. |
| Official package/release | Official Plugins package selector, vendored SDK mapping, source manifest, signed catalog/release bundle, Platform grant and installed host configuration. | Local provider wheels and source tests passed for their listed snapshots; the validation report found official cloud/email package selector/bootstrap absent at the reviewed Plugins snapshot. | `NOT_RUN`; local build is not official package publication or installation. |
| Real operation | Authorized tenant/provider accounts, native host, and user-approved targets. | No live tenant or host operation was used for this evidence set. | WeCom auth-init/OAuth, cloud APIs, real SMTP delivery, browser notification permission, and full Product-to-Navigator flow are `NOT_RUN`. |

This matrix is an evidence cut dated 2026-10-14. It intentionally distinguishes committed source snapshots from later dirty worktrees and newer unreviewed commits. A passing fixture suite proves only the covered local protocol behavior. Do not promote `SIMULATED` to `REAL`, infer a merge from a local commit, or report a deployment until the exact integrated head has passed its own gates.

## Accepted references

- Shared contract seam and fixed Plugin operations: the task-private `INTERFACES.md` and `SDK_API.md` review inputs; public implementation owners are Cyrene-Plugins-Official contracts, TCK, and provider packages. These private artifacts are evidence inputs, not deployable API specifications.
- Product error shape and semantics: [Product HTTP semantics](../governance/product-contract-v1/product-http-semantics.md), [Problem Details schema](../governance/product-contract-v1/problem-details.schema.json), and [contract tests](../governance/product-contract-v1/tests/test_contract.py).
- Platform authority and catalog grants: the active Platform-owned policy, pinned Product catalog, Directory roles, and host workload configuration. Workspace documents these authorities; it does not duplicate them.
- Plugin usage and protocol inventory: [Cyrene Plugin development reference](../.agents/skills/cyrene-plugin-development/references/system-interfaces.md).
- Delivery and integration status: source commits above plus owner PRs, post-merge CI, and canonical remote read-back. The artifact snapshots are private review inputs, not public API specifications.

## 中文：决策、边界与功能范围

复用现有 Product、Platform 和 Plugin 权属。本功能不新增身份服务、云资源目录权威、账号关联数据库或模型驱动的 monitor。

| 事项 | 权属与权威值 | 边界 |
|---|---|---|
| 用户、组织、workspace、Directory role、操作 grant、通用 resource/Lease/Fence authority | Platform | 每次请求都根据当前策略和精确 catalog operation grant 重新授权。Provider observation 不会创建 Platform resource 或 grant。 |
| 外部身份关联 | Plugins Account Center | `accountLinkRef` 标识 Account Center 拥有且已验证外部 actor 的关联。 |
| Provider 原生 profile 或 grant | Provider binding / native provider | `nativeProviderRef`（需要时以范围受限的 `authorizationRef` 暴露）标识 Provider 所有的授权状态。它不是 `accountLinkRef`，不证明外部 actor 已关联，也不是 Client 或调用方提供的凭据。 |
| Product connection、resource observation、monitor 状态、renewal intent、通知序列和发送回执 | Exchange | 持久 Product 状态保存在现有 integration store，只返回 Product 自有引用。Connection 和 Provider binding 从可信服务端配置解析。 |
| Provider 能力和原生凭据 | Plugins | 暴露固定、有类型的操作。CLI profile、OAuth secret、SMTP credential 和 Provider 响应留在其所属 adapter/configuration 边界内。 |
| 任务暂停与继续 | Navigator | 保留现有 task 和进度；不创建第二个 task 数据库，也不重跑之前的 model turn。 |
| 用户呈现 | Client | 呈现 Product 所有且 actor 范围隔离的数据。Client state 不是授权或资源权威。 |

云 Provider ID 和元数据始终是 Provider observation。只有从 Platform 收到引用后，Exchange 才可附带 `platformResourceRef` 或 generation；不得复制、推导或注册云原生 ID 作为 Platform resource。云 API 读取能力不代表拥有 Platform inventory、调度、放置或 lease 权威。

## 中文：Product 与 Plugin 合约

Plugin transport 继续使用 `tool.provider.v1`；integration-support DTO 和签名 caller context 是共享合约类型，不是新的 runtime 协议。Product HTTP 使用 camelCase，Plugin JSON 使用 snake_case。调用方签名覆盖精确的内部 `arguments` 对象；Provider 在读取状态或创建 intent 前验证 method/tool ID、operation、caller workload、audience、binding、issuer/subject、organization/workspace、request ID、payload hash 和短时效。验证密钥来自 binding 所有的 host configuration。

固定 Plugin 方法及 Product 路由与上述英文表格相同。所有调用使用封闭的 `{caller_context, arguments}` envelope。调用方提交的未知字段、operation ID、Provider target、recipient alias、可执行程序路径、profile、URL 或凭据均须拒绝。Product route 是单独、明确 allowlist 的转换；Product operation ID 不会直接转发成 Plugin operation ID。

每个 API 调用都使用现有经验证 bearer principal、organization/workspace 范围、精确 operation permission 和当前 Platform catalog grant。Host configuration pin 活动 Exchange Product workload profile 与 operation catalog；UI 控件或 Provider `authorizationRef` 都不能授予访问权。缺少当前身份、角色、grant、binding 或 host profile 时 fail closed。Platform `AWAITING_AUTHORITY` 与 Product Provider reauthorization 保持区别。

## 中文：HTTP 错误语义

只有已确认且需要用户重新授权的 Provider grant 才映射为 HTTP `409` 与 `EXCHANGE_CONNECTION_REAUTHORIZATION_REQUIRED`。RFC 9457 中可选的 `context` 是封闭对象，只包含经过验证的 `connectionId` 和范围受限的 `authorizationRef`；这些引用用于定位已授权的 Product/Provider binding，不证明身份、角色、凭据持有或新 grant。Schema 会拒绝身份声明、角色、凭据、路径和任意扩展。

HTTP `401` 继续表示调用方身份认证问题。一般 `403` 继续表示授权拒绝；Provider permission denial 若不能证明存在受支持的重新授权流程，也属于此类。网络失败、不支持的协议结果和未知 expiry 继续按现有 endpoint 语义保持未知；不得据此声称已过期/有效，或返回重新授权 `409`。Platform `AWAITING_AUTHORITY` 不是 Provider 授权失效。

## 中文：运行行为与功能边界

| 功能 | 必须行为 | 明确限制 |
|---|---|---|
| Authorization health | `PRESENT` 只表示 binding-owned 本地 CLI profile 已授权。只有指定能力的固定只读探针成功才是 `VALID`。区分确认的 permission denial、expiry、login missing、CLI unavailable 和未知网络/协议结果。未知 expiry 返回 `null`。 | WeCom 当前只证明 `wecom.message.aibot.sessions.list`；不宣称所有 WeCom permission 均有效。Cloud binding 引用不是可续期 grant。 |
| WeCom renewal | 已授权 profile 只使用从已识别 Provider 响应中提取并净化的官方 grant page。不得用 `auth init` 更新现有 profile。固定的 noninteractive/no-browser CLI activation 只用于 profile 缺失时的显式首次引导。绑定 CLI Bot identity，在 CLI/UI 副作用前持久化私有 owner-scoped intent；只有重新探测 capability 且 identity 匹配后才递增 generation。 | 不支持或不安全的 Provider URL 返回范围明确、可操作的错误，其他工具和 job 继续运行。Profile/Bot identity 变化要求显式 relink，不能标记 `SUCCEEDED`。Provider 支持的 QR 页面为 `https://work.weixin.qq.com/ai/qc/gen`，并带固定 source marker 和不透明 code；不接受 redirect 或任意 URL。 |
| Cloud observation 与 monitoring | 按确定性 schedule（默认 15 分钟）轮询固定配置 target 引用；Provider 故障时保留最近 observation 并附带来源 freshness。已知 expiry 提前 24 小时和 1 小时通知，确认失效立即通知，未确认的可操作 incident 每 6 小时重复；按 state/generation/stage 去重，无变化时保持安静。Monitor 和对比路径零 LLM/DSH 调用。 | Provider 原生 ID 不是 Platform resource。未知结果不能标成 `STOPPED`、`VALID` 或 expired。这是只读 inventory，不是 cloud control plane。 |
| Email notification | 使用独立 SMTP connector，endpoint/credential 引用由 host 管理，收件方使用配置的 alias。发送前持久化 `SENDING`；没有最终送达证据时只暴露 `ACCEPTED`、`UNKNOWN` 或 `REJECTED`，不宣称送达。 | Email 不依赖 Client 页面打开。SMTP 接受事务不证明收件人已读取或最终收到。 |
| Browser notification | Client 只能在用户明确操作后请求浏览器 Notification permission。持久 in-app feed 是恢复来源。 | Browser system notification 不是 Web Push，浏览器关闭后不会送达。本方案不包含 push subscription、后台推送或关闭浏览器仍可通知的承诺。 |
| Navigator continuation | 精确 Provider `409` 将相关工作暂停为 `AWAITING_USER_AUTHORIZATION`。actor/workspace 重新验证且 authorization generation 更新后，只继续尚未执行、allowlist 内的只读工作；保留既有 task 和进度。独立 task 继续运行。 | 不重放此前 model turn。未知或可能已完成的写操作必须先 reconciliation/人工处理，绝不自动重放。 |

## 中文：持久恢复规则

| 故障或状态迁移 | 恢复规则 |
|---|---|
| Renewal start 响应丢失或结果不确定 | Exchange 在 Provider 调用前持久化 intent。保留原范围内 `Idempotency-Key`/签名 request ID。按该 request ID 查询状态是只读操作，可恢复同一 session；不得再次启动 CLI authorization。仍有歧义时保持 `UNKNOWN`/manual。 |
| 重用同一 renewal key | 完全相同的 scope 和 payload 返回原 session。同 scope 但 payload 不同则拒绝。Session status 按 binding、actor、organization 和 workspace 校验 owner。 |
| Provider 报告 profile identity 变化 | 失败并要求显式 relink。不得转移旧 binding 的授权，也不得把不同 Bot 标为成功。 |
| Provider health 未知 | 保留最近 observation 并报告 `UNKNOWN`；不猜测 expiry，不触发 409 gate，不清除 grant，也不自动初始化。 |
| SMTP DATA 后发送结果未知 | 持久化 `UNKNOWN`；reconciliation 不得开启第二次 SMTP transaction。明确发生在发送前的拒绝按精确 Provider retry policy 处理。 |
| 授权等待期间 Navigator/Exchange 重启 | 保留原 task 为暂停状态。任何安全继续之前，都重新读取同一 actor-owned authorization 和 generation；继续前先 reconciliation 既有 write。不得重放原 turn 或 mutation。 |
| Client 关闭或实时 feed 中断 | 重新打开时从持久 sequence endpoint 恢复。Browser notification 仅在浏览器 API 可用时尽力发送；独立配置的 email 是关闭浏览器时的另一通道。 |

## 中文：部署与验收矩阵

下列任何阶段只有在各 owner 的正常 PR 合并、合并后远端回读、Platform policy/catalog packaging 和所需 host/tenant 检查完成后，才视为生产部署。

| 层 | Host 配置与部署边界 | 证据快照 | 当前门禁 |
|---|---|---|---|
| Shared Plugin contracts | `cyrene_integration_contracts` v0.1.0、签名 context verifier、schemas 和 `tool.provider.v1` TCK。 | `9f3ef7f617aaad0b1cf66d25056ae258de45b6a9` 上 shared TCK 通过 10 项。后续 contracts worktree tip 为 `f623facb012532b9d936dcd3fb4f6c269371eb6c`，存在未提交 TCK/SDK/packager 修改；旧归档验证未覆盖这些修改。 | 仅本地合约证据；release packaging 和当前脏改动需 owner 验证。 |
| WeCom provider | Binding-owned 隔离 CLI profile、来自 host configuration 的 32-byte caller-context key、固定 CLI executable/profile 和 private binding-scoped renewal store。不得把 CLI credential 复制到 Exchange。 | `c7234c26a400c1d209d9938c3412da1def459508`；交叉验证归档报告 87 个 offline tests，owner 后续在相同已提交工作树上使用固定 Navigator fixture 跑过 90 项。 | 仅模拟验证。真实 `wecom-cli`、auth-init、tenant grant 和 Provider API 为 `NOT_RUN`。 |
| Cloud provider | 固定 Provider/target binding、Provider-owned secret reference、有界 HTTPS 读取，不接受调用方选择 URL/account。 | `3ff7c821dbb8c1cd50ad93c5f2d8a24a2eed23fe`；34 个 fixture tests、lint/format、lock check 和本地 package build 通过。 | 仅模拟验证；Azure/GCP/HF/Runpod/Tailscale 调用均 `NOT_RUN`。 |
| SMTP provider | 独立 connector、host-owned SMTP 配置与 secret reference、recipient alias allowlist。 | `c53bf6322e7a74d579f1dc785e65e248e3a6b3a1`；24 个 loopback SMTP protocol tests 和本地 package checks 通过。 | 真实 SMTP send 和 delivery 为 `NOT_RUN`。 |
| Exchange Product API/worker | 独立 integration store/worker；每次调用使用现有 bearer principal 和 Platform 授权的 catalog operation。Host 外部配置当前 Product workload profile、Provider binding 引用、email profile 和 pinned operation catalog。Gateway-only readiness 保持独立。 | `caa92a8c426212c61c253853614073881ae63c86`；36 个 focused component tests 通过。该精确快照尚未接入 production builder；后续 worktree 有未提交 follow-up code，旧验证未覆盖。 | Production builder/current-context/profile/provider/email wiring 及 host-config 验证仍 `NOT_READY`。 |
| Platform policy 与 Workspace bundle | Platform host configuration 必须 pin Exchange workload profile、Product catalog snapshot、精确 operation grant、Directory role、owner binding、workspace 和 release lock。缺少条目时保持 default-deny。 | Platform `origin/develop` 快照 `b294fe9e360c758e516c0b7d2c0a2291d0e1f299`；源码审查发现当前 policy/catalog lock 缺少新 operation ID。 | Feature grant 和 staged signed bundle 待处理；源码检查不等于 grant acceptance。 |
| Navigator | 使用现有 Work API、task persistence、owner check、pause state、read-only child guard 和 generation revalidation。 | `a49c558e7d8e55b988e48993a5ba46ee28497d78`；18 个 Python、34 个 Node tests 和 TypeScript compile 通过。Node 实际版本 v22.23.2，而 source lock 指定 v24.13.0。 | 仅模拟 consumer tests；live DSH、Exchange authority、OAuth 和 Provider runtime 为 `NOT_RUN`。 |
| Client | 使用现有 paired Control session；只对支持的 WeCom 暴露 renewal；使用 Product feed；仅显式 UI 操作请求 browser permission；link 限定到 WeCom 固定 origin/path/query。 | 审核提交 `a5189d84ab012220729a63d4bf4cc28f96f8a02b`（11 个 focused tests；Node 22 build smoke）。当前 worker branch tip `031be23683add9f2d6a0073199613ff30d1684dd` 加入 same-key recovery、provider gate、固定 URL validation 和 nullable feed ID；归档 review 早于该 tip。 | 最新 Client 代码需要精确 head 验证；browser permission、paired live session 和端到端 UI 为 `NOT_RUN`。 |
| Official package/release | Official Plugins package selector、vendored SDK mapping、source manifest、签名 catalog/release bundle、Platform grant 与安装 host configuration。 | 列出的 provider 源快照通过本地 source tests 和 package build；验证报告在其检查的 Plugins snapshot 上发现官方 cloud/email package selector/bootstrap 缺失。 | `NOT_RUN`；本地 build 不等于官方 package 发布或安装。 |
| 真实操作 | 已授权 tenant/provider accounts、原生 host、用户批准的目标。 | 本组证据没有使用实时 tenant 或 host 操作。 | WeCom auth-init/OAuth、cloud API、真实 SMTP delivery、browser notification permission 和完整 Product-to-Navigator flow 均为 `NOT_RUN`。 |

本矩阵是截至 2026-10-14 的证据快照，刻意区分已提交源码、后续脏工作树和更新但尚未审核的提交。fixture suite 只证明其覆盖的本地协议行为。不得把 `SIMULATED` 提升成 `REAL`，不得从本地提交推断已合并，也不得在精确集成 head 通过自身门禁前报告部署完成。

## 合约引用

- Shared contract seam 和固定 Plugin operations：任务私有的 `INTERFACES.md`、`SDK_API.md` review 输入；公开实现由 Cyrene-Plugins-Official contracts、TCK 和 provider packages 所有。这些私有 artifact 是证据输入，不是可部署 API 规范。
- Product error shape/semantics：[Product HTTP semantics](../governance/product-contract-v1/product-http-semantics.md)、[Problem Details schema](../governance/product-contract-v1/problem-details.schema.json) 和 [contract tests](../governance/product-contract-v1/tests/test_contract.py)。
- Platform authority 与 catalog grant：使用 Platform 所有的活动 policy、pinned Product catalog、Directory role 和 host workload configuration。Workspace 记录这些权属，不复制它们。
- Plugin 使用方式与协议 inventory：[Cyrene Plugin development reference](../.agents/skills/cyrene-plugin-development/references/system-interfaces.md)。
- 交付和集成状态：以上源码提交加 owner PR、合并后 CI 和 canonical remote read-back。Artifact snapshots 是私有 review 输入，不是公开 API 规范。
