# ADR: Authorization, Cloud Observation, and Recovery v1

- **Status:** Design and local implementation evidence recorded; cross-repository integration and deployment acceptance are pending.
- **Date:** 2026-10-14
- **Scope:** Provider authorization health and user-led renewal, configured cloud-resource observations, durable notifications, and safe Navigator continuation.
- **Decision owners:** Platform owns Directory, policy, catalog grants, and generic resource authority; Plugins owns provider capabilities and native credentials; Exchange owns Product connections, observations, renewal jobs, and notifications; Navigator owns task continuation; Client owns presentation and explicit browser notification permission.

This ADR records the shared v1 boundary and the exact implementation snapshots reviewed for this task. It does not claim that the feature branches are merged, that the deployment is wired, or that renewal, message sending, or a full real-tenant workflow passed. One local read-only WeCom health observation is recorded below. Each source SHA identifies evidence only for that snapshot; newer commits require their own checks.

本 ADR 记录 v1 共享边界及本任务审核过的精确实现快照，不表示功能分支已合并、部署已接通，也不表示续权、消息发送或真实租户完整流程已通过。下文记录了一次本机只读 WeCom health observation。每个源码 SHA 仅代表对应快照的证据；更新提交须重新检查。

## Decision and authority boundaries

Use existing Product, Platform, and Plugin owners. This feature adds no duplicate identity service, cloud inventory authority, account-link database, or model-driven monitor.

| Concern | Owner and authoritative value | Boundary |
|---|---|---|
| User, organization, workspace, Directory role, operation grant, and generic resource/Lease/Fence authority | Platform | Resolve current policy and exact catalog operation grants for every request. A provider observation does not create a Platform resource or grant. |
| External identity association | Plugins Account Center | `accountLinkRef` identifies an Account Center-owned link and verified external actor. |
| Native provider profile or grant | Provider binding / native provider | `nativeProviderRef` (surfaced as a scoped `authorizationRef` where needed) identifies provider-owned authorization state. It is not an `accountLinkRef`, does not prove a linked external actor, and is not a Client or caller-provided credential. |
| Product connection, resource observation, monitor state, renewal intent, notification sequence, and send receipt | Exchange | Keep durable Product state in the existing integration store and return only Product-owned references. Resolve connection and provider binding from trusted server configuration. |
| Provider capability and native credentials | Plugins | Expose fixed, typed operations. Keep CLI profiles, OAuth secrets, SMTP credentials, and provider responses inside their owning adapter/configuration boundary. |
| Task pause and continuation | Navigator | Keep the original task at `awaiting_user_authorization`. A new explicit bounded read-only prompt may create a separately owned child continuation; never resume or replay the original Agent turn. |
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
| `GET /api/v1/accounts/renewal-sessions/{session_id}` | Read one actor-owned Product renewal-session resource by its stable UUID. The `202 Location` points to this same flat path; the server can reconcile an ambiguous provider start internally. |
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
| Navigator continuation | Exact provider `409` leaves the original task in the wire state `awaiting_user_authorization`. Continuation requires a new explicit bounded read-only prompt. Navigator atomically creates a child task with `continuationOfTaskId` and `continuationMode=read_only`, persisting its metadata before exposing it as queued. Revalidate the current actor/workspace and newer authorization generation. Independent tasks continue. | Do not resume or replay the original Agent turn, inherit its approvals, or automatically continue the original task. Reject writes, unclassified tools, shell, and subagents in the child. Unknown or possibly completed writes remain blocked for reconciliation/manual resolution. |

## Durable recovery rules

| Failure or transition | Recovery rule |
|---|---|
| Renewal start response is lost or ambiguous | Exchange persists an intent before the provider call. Retain the original scoped `Idempotency-Key`; an exact POST replay resolves the same Product session, then read its `Location` at `/api/v1/accounts/renewal-sessions/{session_id}` using the single session UUID. The server may reconcile a missing provider session reference with the original signed request ID; callers do not place that request ID in the GET path. Never start a second CLI authorization. If reconciliation remains ambiguous, leave it `UNKNOWN`/manual. |
| Same renewal key is repeated | Exact same scope and payload returns the original session. Same scope with a changed payload is rejected. Session status is owner-checked against binding, actor, organization, and workspace. |
| Provider reports a changed profile identity | Fail the renewal and require an explicit relink. Never transfer the prior binding's authorization or mark a different Bot as success. |
| Provider health is unknown | Preserve the prior observation and report `UNKNOWN`; do not guess expiry, raise the 409 gate, clear a grant, or auto-run initialization. |
| SMTP outcome may have followed DATA | Persist `UNKNOWN`; reconcile without another SMTP transaction. A known pre-send rejection may be handled under the exact provider retry policy. |
| Navigator/Exchange restarts during an authorized wait | Preserve the original task as `awaiting_user_authorization`; a restart never resumes its Agent turn. Only a new user-supplied bounded read-only prompt can create a child continuation. Revalidate actor/workspace and authorization generation, then atomically persist child metadata (`continuationOfTaskId`, `continuationMode=read_only`) before making it visible as queued. Reject writes, unclassified tools, shell, subagents, and inherited approvals; reconcile unknown prior writes before any further work. |
| Client closes or loses its live feed | On reopen, recover from the durable sequence endpoint. Browser notifications are best-effort only while browser APIs are available; independent configured email is the separate closed-browser channel. |

## Deployment and acceptance matrix

No stage below is considered production-deployed until normal owner PR merges, post-merge remote read-back, Platform policy/catalog packaging, and the required host/tenant checks are complete.

| Layer | Host configuration and deployment boundary | Evidence snapshot | Current gate |
|---|---|---|---|
| Shared Plugin contracts | `cyrene_integration_contracts` v0.1.0, signed-context verifier, schemas, and `tool.provider.v1` TCK. | Exact Plugins source `0ad236a0860a39926679ccbe8d3a9ede8c1495ca`, pushed as Cyrene-Plugins-Official PR #124. TCK: 10 passed; release metadata: 6 tests plus 10 subtests; complete clean-start/release tests: 2 tests plus 9 subtests. Wheel build, source-manifest, and hygiene checks passed locally. | Source validation passed; PR hosted CI and normal merge/read-back remain pending. No vendor credentials or endpoints were used. |
| WeCom provider | Binding-owned isolated CLI profile, 32-byte caller-context key from host configuration, fixed CLI executable/profile, and private binding-scoped renewal store. Do not copy CLI credentials into Exchange. | `c7234c26a400c1d209d9938c3412da1def459508`; 90 offline tests passed with a pinned Navigator fixture. A separate owner-run local read-only health observation against an existing profile returned login `PRESENT`, capability `wecom.message.aibot.sessions.list` `VALID`, and `expiresAt=null`; the private artifact records the result. No `auth init` or message send occurred. | One local capability observation only; it does not prove every WeCom grant. Real renewal/auth-init, message send, and tenant end-to-end acceptance remain `NOT_RUN`. |
| Cloud provider | Fixed provider/target bindings, provider-owned secret references, bounded HTTPS reads, and no caller-selected URL/account. | `3ff7c821dbb8c1cd50ad93c5f2d8a24a2eed23fe`; 34 fixture tests, lint/format, lock check, and local package build passed. | Simulated only; Azure/GCP/HF/Runpod/Tailscale calls are `NOT_RUN`. |
| SMTP provider | Independent connector, host-owned SMTP configuration and secret references, recipient alias allowlist. | `c53bf6322e7a74d579f1dc785e65e248e3a6b3a1`; 24 loopback SMTP protocol tests and local package checks passed. | Real SMTP send and delivery are `NOT_RUN`. |
| Exchange Product API/worker | Separate integration store and workers; use existing bearer principal and Platform-authorized catalog operation on every call. Configure the current host Product workload profile, provider binding refs, email profile, and pinned operation catalog outside request JSON. Gateway-only readiness remains independent. | Committed source `caa92a8c426212c61c253853614073881ae63c86` passed 36 focused component tests. The current owner source updates the status resource to `GET /api/v1/accounts/renewal-sessions/{session_id}` with one session UUID; that route change awaits its exact committed snapshot and hosted validation. | `NOT_READY` for production builder/current-context/profile/provider/email wiring and host-config exercise; flat-GET source integration is pending exact-head proof. |
| Platform policy and Workspace bundle | Platform host configuration must pin the Exchange workload profile, Product catalog snapshot, exact operation grants, Directory roles, owner bindings, workspace, and release lock. Missing entries remain default-deny. | Platform `origin/develop` snapshot `b294fe9e360c758e516c0b7d2c0a2291d0e1f299`; source review found the new operation IDs absent from current policy/catalog lock. | Feature grants and staged signed bundle are pending; source review is not grant acceptance. |
| Navigator | Existing Work API, task persistence, ownership checks, pause state, read-only child guard, and generation revalidation. | `a49c558e7d8e55b988e48993a5ba46ee28497d78`; 18 Python and 34 Node tests plus TypeScript compile passed. The Node run was v22.23.2 while the source lock specifies v24.13.0. The flat-session GET consumer update is still pending its exact final source snapshot. | Prior simulated consumer tests only; final flat-GET consumer check, live DSH, Exchange authority, OAuth, and provider runtime are `NOT_RUN`. |
| Client | Existing paired Control session; expose WeCom renewal only when supported; use Product feed; request browser notification permission from explicit UI action; restrict links to the WeCom fixed origin/path/query. | Source `9ba69fd029fe359506ebe6bd330c36b98d80d88a` polls the flat Product UUID resource. Node 24 tests (49), source CI, and normal merge/read-back passed; merge commit is `abe62fc38b23314507635b239bb2a617809280dd`. | Browser notification permission, paired live session, and end-to-end UI remain `NOT_RUN`. |
| Official package/release | Official Plugins source manifest, release metadata, signed catalog/release bundle, Platform grant, and installed host configuration. | Shared SDK/release metadata and clean-start fixtures pass on PR #124 (`0ad236a0860a39926679ccbe8d3a9ede8c1495ca`); provider package tests/builds are local evidence only. | Exact-head hosted CI, normal PR merge, published release, Platform grant, and installed-host acceptance remain pending. |
| Real operation | Authorized tenant/provider accounts, native host, and user-approved targets. | One owner-run local read-only WeCom profile health observation is recorded in the provider row; no full tenant/host end-to-end was used. | WeCom renewal/auth-init/OAuth and message send, cloud APIs, real SMTP delivery, browser notification permission, and full Product-to-Navigator flow are `NOT_RUN`. |

This matrix is an evidence cut dated 2026-10-14. It reports only named source snapshots and task-artifact test results; ongoing owner edits are not treated as accepted evidence. A passing fixture suite proves only the covered local protocol behavior. Do not promote `SIMULATED` to `REAL`, infer a merge from a local commit or PR, or report a deployment until the exact integrated head has passed its own gates and remote read-back.

## Accepted references

- Shared contract seam and fixed Plugin operations: the task-private `INTERFACES.md` and `SDK_API.md` review inputs; public implementation owners are Cyrene-Plugins-Official contracts, TCK, and provider packages. These private artifacts are evidence inputs, not deployable API specifications. Product route authority remains Exchange's current `contracts/product/v1/openapi.yaml` and API source; the initial handoff path for renewal GET is superseded by the flat UUID resource recorded above.
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
| 任务暂停与继续 | Navigator | 原 task 保持 `awaiting_user_authorization`。只有用户新提交明确、受限的只读 prompt 后，才创建独立的 child continuation；不得恢复或重放原 Agent turn。 |
| 用户呈现 | Client | 呈现 Product 所有且 actor 范围隔离的数据。Client state 不是授权或资源权威。 |

云 Provider ID 和元数据始终是 Provider observation。只有从 Platform 收到引用后，Exchange 才可附带 `platformResourceRef` 或 generation；不得复制、推导或注册云原生 ID 作为 Platform resource。云 API 读取能力不代表拥有 Platform inventory、调度、放置或 lease 权威。

## 中文：Product 与 Plugin 合约

Plugin transport 继续使用 `tool.provider.v1`；integration-support DTO 和签名 caller context 是共享合约类型，不是新的 runtime 协议。Product HTTP 使用 camelCase，Plugin JSON 使用 snake_case。调用方签名覆盖精确的内部 `arguments` 对象；Provider 在读取状态或创建 intent 前验证 method/tool ID、operation、caller workload、audience、binding、issuer/subject、organization/workspace、request ID、payload hash 和短时效。验证密钥来自 binding 所有的 host configuration。

固定 Plugin 方法及 Product 路由与上述英文表格相同。Renewal GET 的规范资源为 `GET /api/v1/accounts/renewal-sessions/{session_id}`，其中 `{session_id}` 是单个 Product session UUID，`202 Location` 返回同一资源路径。所有调用使用封闭的 `{caller_context, arguments}` envelope。调用方提交的未知字段、operation ID、Provider target、recipient alias、可执行程序路径、profile、URL 或凭据均须拒绝。Product route 是单独、明确 allowlist 的转换；Product operation ID 不会直接转发成 Plugin operation ID。

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
| Navigator continuation | 精确 Provider `409` 使原 task 保持 wire state `awaiting_user_authorization`。继续工作必须由用户重新提交明确、受限的只读 prompt。Navigator 原子创建 child task，设置 `continuationOfTaskId` 和 `continuationMode=read_only`，并在将其显示为 queued 前先持久化 metadata。重新验证当前 actor/workspace 与更新后的 authorization generation。独立 task 继续运行。 | 不恢复或重放原 Agent turn，不继承原审批，也不自动继续原 task。Child 必须拒绝写操作、未分类工具、shell 和 subagent。未知或可能已完成的写操作继续阻塞，直到 reconciliation/人工处理。 |

## 中文：持久恢复规则

| 故障或状态迁移 | 恢复规则 |
|---|---|
| Renewal start 响应丢失或结果不确定 | Exchange 在 Provider 调用前持久化 intent。保留原范围内 `Idempotency-Key`；完全相同的 POST replay 返回同一 Product session，再从其 `Location` 用单一 session UUID 访问 `/api/v1/accounts/renewal-sessions/{session_id}`。若 Provider session 引用尚未保存，服务端可用原签名 request ID 做只读 reconciliation；调用方不把 request ID 放入 GET 路径。不得再次启动 CLI authorization。仍有歧义时保持 `UNKNOWN`/manual。 |
| 重用同一 renewal key | 完全相同的 scope 和 payload 返回原 session。同 scope 但 payload 不同则拒绝。Session status 按 binding、actor、organization 和 workspace 校验 owner。 |
| Provider 报告 profile identity 变化 | 失败并要求显式 relink。不得转移旧 binding 的授权，也不得把不同 Bot 标为成功。 |
| Provider health 未知 | 保留最近 observation 并报告 `UNKNOWN`；不猜测 expiry，不触发 409 gate，不清除 grant，也不自动初始化。 |
| SMTP DATA 后发送结果未知 | 持久化 `UNKNOWN`；reconciliation 不得开启第二次 SMTP transaction。明确发生在发送前的拒绝按精确 Provider retry policy 处理。 |
| 授权等待期间 Navigator/Exchange 重启 | 原 task 继续保持 `awaiting_user_authorization`；重启不恢复 Agent turn。只有用户提交新的受限只读 prompt 后，才能创建 child continuation。重新验证 actor/workspace 和 authorization generation，再原子持久化 child metadata（`continuationOfTaskId`、`continuationMode=read_only`），之后才显示为 queued。拒绝写操作、未分类工具、shell、subagent 和继承审批；后续工作前先 reconciliation 未知的既有 write。 |
| Client 关闭或实时 feed 中断 | 重新打开时从持久 sequence endpoint 恢复。Browser notification 仅在浏览器 API 可用时尽力发送；独立配置的 email 是关闭浏览器时的另一通道。 |

## 中文：部署与验收矩阵

下列任何阶段只有在各 owner 的正常 PR 合并、合并后远端回读、Platform policy/catalog packaging 和所需 host/tenant 检查完成后，才视为生产部署。

| 层 | Host 配置与部署边界 | 证据快照 | 当前门禁 |
|---|---|---|---|
| Shared Plugin contracts | `cyrene_integration_contracts` v0.1.0、签名 context verifier、schemas 和 `tool.provider.v1` TCK。 | Exact source `0ad236a0860a39926679ccbe8d3a9ede8c1495ca` / Cyrene-Plugins-Official PR #124；TCK 10 项通过，release metadata 6 tests + 10 subtests，complete clean-start/release 2 tests + 9 subtests 通过。Wheel、source-manifest 和 hygiene 检查通过。 | 源码验证通过；PR hosted CI 和正常合并/回读待完成。未使用真实 credential 或 Provider endpoint。 |
| WeCom provider | Binding-owned 隔离 CLI profile、来自 host configuration 的 32-byte caller-context key、固定 CLI executable/profile 和 private binding-scoped renewal store。不得把 CLI credential 复制到 Exchange。 | `c7234c26a400c1d209d9938c3412da1def459508`；使用固定 Navigator fixture 的 90 个 offline tests 通过。Owner 对既有 profile 执行一次本机只读 health observation：login `PRESENT`、`wecom.message.aibot.sessions.list` capability `VALID`、`expiresAt=null`；结果保存在私有 artifact。未执行 `auth init` 或发送消息。 | 仅有一次本机 capability observation，不证明所有 WeCom grant。真实 renewal/auth-init、消息发送和租户端到端验收仍为 `NOT_RUN`。 |
| Cloud provider | 固定 Provider/target binding、Provider-owned secret reference、有界 HTTPS 读取，不接受调用方选择 URL/account。 | `3ff7c821dbb8c1cd50ad93c5f2d8a24a2eed23fe`；34 个 fixture tests、lint/format、lock check 和本地 package build 通过。 | 仅模拟验证；Azure/GCP/HF/Runpod/Tailscale 调用均 `NOT_RUN`。 |
| SMTP provider | 独立 connector、host-owned SMTP 配置与 secret reference、recipient alias allowlist。 | `c53bf6322e7a74d579f1dc785e65e248e3a6b3a1`；24 个 loopback SMTP protocol tests 和本地 package checks 通过。 | 真实 SMTP send 和 delivery 为 `NOT_RUN`。 |
| Exchange Product API/worker | 独立 integration store/worker；每次调用使用现有 bearer principal 和 Platform 授权的 catalog operation。Host 外部配置当前 Product workload profile、Provider binding 引用、email profile 和 pinned operation catalog。Gateway-only readiness 保持独立。 | `caa92a8c426212c61c253853614073881ae63c86`；36 个 focused component tests 通过。该精确快照尚未接入 production builder；后续 worktree 有未提交 follow-up code，旧验证未覆盖。 | Production builder/current-context/profile/provider/email wiring 及 host-config 验证仍 `NOT_READY`。 |
| Platform policy 与 Workspace bundle | Platform host configuration 必须 pin Exchange workload profile、Product catalog snapshot、精确 operation grant、Directory role、owner binding、workspace 和 release lock。缺少条目时保持 default-deny。 | Platform `origin/develop` 快照 `b294fe9e360c758e516c0b7d2c0a2291d0e1f299`；源码审查发现当前 policy/catalog lock 缺少新 operation ID。 | Feature grant 和 staged signed bundle 待处理；源码检查不等于 grant acceptance。 |
| Navigator | 使用现有 Work API、task persistence、owner check、pause state、read-only child guard 和 generation revalidation。 | `a49c558e7d8e55b988e48993a5ba46ee28497d78`；18 个 Python、34 个 Node tests 和 TypeScript compile 通过。Node 实际版本 v22.23.2，而 source lock 指定 v24.13.0。 | 仅模拟 consumer tests；live DSH、Exchange authority、OAuth 和 Provider runtime 为 `NOT_RUN`。 |
| Client | 使用现有 paired Control session；只对支持的 WeCom 暴露 renewal；使用 Product feed；仅显式 UI 操作请求 browser permission；link 限定到 WeCom 固定 origin/path/query。 | Source `9ba69fd029fe359506ebe6bd330c36b98d80d88a` 使用 flat Product UUID resource。Node 24 下 49 tests、source CI 和正常 merge/read-back 通过；merge commit 为 `abe62fc38b23314507635b239bb2a617809280dd`。 | Browser notification permission、paired live session 和端到端 UI 仍为 `NOT_RUN`。 |
| Official package/release | Official Plugins source manifest、release metadata、签名 catalog/release bundle、Platform grant 与安装 host configuration。 | Shared SDK/release metadata 和 clean-start fixtures 在 PR #124 (`0ad236a0860a39926679ccbe8d3a9ede8c1495ca`) 通过；provider package tests/builds 是本地证据。 | Exact-head hosted CI、正常合并、published release、Platform grant 和 installed-host acceptance 仍待完成。 |
| 真实操作 | 已授权 tenant/provider accounts、原生 host、用户批准的目标。 | WeCom provider 行记录了一次 owner 本机只读 profile health observation；没有执行完整 tenant/host 端到端。 | WeCom renewal/auth-init/OAuth 和消息发送、cloud API、真实 SMTP delivery、browser notification permission 和完整 Product-to-Navigator flow 均为 `NOT_RUN`。 |

本矩阵是截至 2026-10-14 的证据快照，仅报告具名源码快照与任务 artifact 中的测试结果；owner 正在进行的编辑不视为通过。fixture suite 只证明其覆盖的本地协议行为。不得把 `SIMULATED` 提升成 `REAL`，不得从本地提交或 PR 推断已合并，也不得在精确集成 head 通过自身门禁并完成远端回读前报告部署完成。

## 合约引用

- Shared contract seam 和固定 Plugin operations：任务私有的 `INTERFACES.md`、`SDK_API.md` review 输入；公开实现由 Cyrene-Plugins-Official contracts、TCK 和 provider packages 所有。这些私有 artifact 是证据输入，不是可部署 API 规范。Product route 以 Exchange 当前的 `contracts/product/v1/openapi.yaml` 和 API source 为准；handoff 中最初的 renewal GET 路径已由上文记录的 flat UUID resource 取代。
- Product error shape/semantics：[Product HTTP semantics](../governance/product-contract-v1/product-http-semantics.md)、[Problem Details schema](../governance/product-contract-v1/problem-details.schema.json) 和 [contract tests](../governance/product-contract-v1/tests/test_contract.py)。
- Platform authority 与 catalog grant：使用 Platform 所有的活动 policy、pinned Product catalog、Directory role 和 host workload configuration。Workspace 记录这些权属，不复制它们。
- Plugin 使用方式与协议 inventory：[Cyrene Plugin development reference](../.agents/skills/cyrene-plugin-development/references/system-interfaces.md)。
- 交付和集成状态：以上源码提交加 owner PR、合并后 CI 和 canonical remote read-back。Artifact snapshots 是私有 review 输入，不是公开 API 规范。
