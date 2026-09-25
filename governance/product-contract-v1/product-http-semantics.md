# Cyrene Product HTTP and evolution profile v1

This document is the single cross-Product authority for HTTP evolution, errors,
idempotency, asynchronous actions, and trace propagation. Product OpenAPI files
declare `x-cyrene-contract-profile: product-http-v1` and define only their own
resources, commands, states, and Product-specific error codes.

## Identity

- Product resource IDs are stable and opaque. UUID wire format is preferred;
  an explicitly opaque string is allowed where a pre-existing Workspace ID is
  preserved.
- API identity never exposes a database row key, filesystem path, PID, worker,
  lease, fence token, executable, provider identity, or plugin package identity.
- A Product resource ID is never a Kernel Operation ID. `DeploymentId` and
  `EndpointId`, and capability `BindingId` and provider identity, remain
  different concepts.
- Cross-Product fields are references to owner resources; they do not transfer
  resource authority to the consumer.

## Compatibility and deprecation

- Major: any breaking semantic or wire change, including changed state meaning,
  removed fields or enum values, or a newly required input.
- Minor: backward-compatible optional fields, operations, and enum values whose
  unknown handling is documented. Old consumers must ignore unknown optional
  object fields.
- Patch: semantic-preserving correction or clarification only.
- OpenAPI operations, parameters, or schemas begin deprecation with
  `deprecated: true` and a migration link. Removal is allowed only in the next
  major version after at least 180 days and one published minor-version
  migration window. A security issue may shorten the window only with a
  published exception and replacement path.

## Problem Details

Product control APIs use RFC 9457 `application/problem+json`. The canonical
schema is `problem-details.schema.json` and requires `type`, `title`, `status`,
`detail`, `instance`, stable uppercase `code`, `retryable`, and W3C-derived
`traceId`; `resourceRef` is optional. Generated Product copies must be byte-for-
byte semantically equal to this schema and are not authorities.

Problem payloads must not expose filesystem paths, PIDs, SQL text, raw
exceptions, secrets, tokens, worker executables, provider package internals, or
stack traces. Product resource failure records use stable codes and sanitized
messages; rejected HTTP commands use Problem Details.

## Asynchronous actions

RFC 7240 defines the preference, while this profile completes the lifecycle:

1. A supported action receiving `Prefer: respond-async` returns `202 Accepted`,
   `Preference-Applied: respond-async`, and `Location` naming the stable
   Product-owned resource used for polling.
2. `GET Location` returns current Product state, resource version, terminal
   state, and a sanitized Product failure when applicable. The location never
   names or aliases a Kernel Operation.
3. Cancellation is a Product command on that resource. Acceptance returns
   `202` and the same Product `Location`; cancellation intent is not terminal
   proof. A terminal state requires reconciled execution evidence.
4. Transport retry reuses the same `Idempotency-Key`. Product policy retry uses
   the documented Product Run/Attempt/Generation model and never reuses a
   Kernel lease or fence as HTTP idempotency.
5. `traceparent` and optional `tracestate` correlate the request, response, and
   downstream calls. Product ID, idempotency key, and trace ID are distinct.

Synchronous reference MVP actions return `201` only after their Product
resource reaches the represented state. They do not advertise async behavior
that they have not implemented.

## Idempotency

`Idempotency-Key` is a Cyrene-defined request header because the IETF draft is
not a stable RFC. Scope is authenticated principal plus operation plus owning
resource where applicable. A replay with the same key and canonical request
body returns the current representation of the same Product resource and the
operation's normal success status (`201` or `202`). Reuse with a different body
returns `409` and a stable Product-specific conflict code.

The mapping is retained at least for the lifetime of the referenced durable
resource; Products with deletion must publish a longer bounded retention policy
before expiring keys. HTTP idempotency never substitutes for Kernel lease,
generation, or fence correctness.

## Trace Context

The normative dependency is W3C Trace Context Level 1, Recommendation of
23 November 2021: `traceparent` and `tracestate`. All-zero trace or parent IDs
and syntactically invalid version `00` values are rejected as incoming identity;
the service starts a new valid context rather than propagating them. Trace
Context Level 2 is only forward-compatible experimental support until it becomes
a W3C Recommendation and is not a Product v1 normative dependency.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene Product HTTP 与演进规范 v1

本文是所有 Product 共用的 HTTP 演进、错误、幂等、异步操作和追踪传播的唯一权威文件。Product OpenAPI 文件声明 `x-cyrene-contract-profile: product-http-v1`，并且只定义自身资源、命令、状态和 Product 专属错误码。

## 身份

- Product 资源 ID 稳定且不透明。优先使用 UUID 在线格式；保留既有 Workspace ID 时，也允许显式采用不透明字符串。
- API 身份绝不暴露数据库行键、文件系统路径、PID、Worker、租约、fence token、可执行文件、Provider 身份或插件包身份。
- Product 资源 ID 不等同于 Kernel Operation ID。`DeploymentId` 与 `EndpointId`，以及能力 `BindingId` 与 Provider 身份，仍是不同概念。
- 跨 Product 字段是指向资源所属方的引用；它们不会把资源权威转移给消费者。

## 兼容性与弃用

- **Major**：任何破坏性语义或在线格式变更，包括状态含义改变、字段或枚举值移除，或新增必填输入。
- **Minor**：向后兼容的可选字段、操作和枚举值，且已记录如何处理未知值。旧版消费者必须忽略未知的可选对象字段。
- **Patch**：只允许不改变语义的修正或澄清。
- OpenAPI 操作、参数或 schema 开始弃用时，须设置 `deprecated: true` 并提供迁移链接。只有在下一个 major 版本、至少经过 180 天且提供一个已发布 minor 版本迁移窗口后，才可移除。安全问题只能通过公开的例外说明和替代路径缩短该窗口。

## Problem Details

Product 控制 API 使用 RFC 9457 `application/problem+json`。规范 schema 为 `problem-details.schema.json`，并要求包含 `type`、`title`、`status`、`detail`、`instance`、稳定的大写 `code`、`retryable` 和基于 W3C 的 `traceId`；`resourceRef` 是可选项。生成到各 Product 的副本在语义上必须与此 schema 完全相同，且不具有权威性。

问题载荷不得暴露文件系统路径、PID、SQL 文本、原始异常、秘密、令牌、Worker 可执行文件、Provider 包内部信息或堆栈跟踪。Product 资源失败记录使用稳定代码和经过脱敏的消息；被拒绝的 HTTP 命令使用 Problem Details。

## 异步操作

RFC 7240 定义了该偏好头；本规范补全其生命周期：

1. 支持异步的操作收到 `Prefer: respond-async` 后，返回 `202 Accepted`、`Preference-Applied: respond-async`，以及一个 `Location`，指向用于轮询的稳定 Product 自有资源。
2. `GET Location` 返回当前 Product 状态、资源版本、终态，以及适用时经脱敏的 Product 失败信息。此位置绝不能指向或别名映射到 Kernel Operation。
3. 取消是针对该资源发出的 Product 命令。接受取消时返回 `202` 和相同的 Product `Location`；取消意图不构成终态证明。只有经协调确认的执行证据才能证明终态。
4. 传输重试必须重用相同的 `Idempotency-Key`。Product 策略重试使用文档规定的 Product Run/Attempt/Generation 模型，绝不把 Kernel 租约或 fence 当作 HTTP 幂等键。
5. `traceparent` 和可选的 `tracestate` 用于关联请求、响应和下游调用。Product ID、幂等键和 trace ID 各自独立。

同步参考 MVP 操作只有在 Product 资源达到其所表示的状态后才返回 `201`。它们不会宣称自己支持尚未实现的异步行为。

## 幂等

由于 IETF 草案尚不是稳定 RFC，`Idempotency-Key` 是 Cyrene 自定义的请求头。作用域为经过身份验证的主体、操作，以及适用时的所属资源。相同键和规范化请求体的重放会返回同一 Product 资源的当前表示，并返回操作通常的成功状态（`201` 或 `202`）。使用相同键但请求体不同时，返回 `409` 和稳定的 Product 专属冲突代码。

映射至少保留到被引用的持久化资源整个生命周期结束；支持删除的 Product 必须在过期键之前公布更长且有界的保留策略。HTTP 幂等不能替代 Kernel 租约、代次或 fence 的正确性保障。

## Trace Context

规范依赖是 W3C Trace Context Level 1（2021 年 11 月 23 日发布的 Recommendation），即 `traceparent` 和 `tracestate`。全零 trace / parent ID，以及语法无效的版本 `00` 值，作为传入身份时会被拒绝；服务会启动新的有效上下文，而不会传播它们。Trace Context Level 2 在成为 W3C Recommendation 之前，只作为向前兼容的实验性支持，不属于 Product v1 的规范依赖。
