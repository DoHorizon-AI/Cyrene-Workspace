# Cyrene Connector Package Spec v0.1

## Purpose

This is the smallest package contract for a replaceable connector that is
resolved through the existing Platform PluginManifest, PluginSetLock, and
node-local PluginStore seams. It describes how a connector can be made
available and verified; it does not define a marketplace, a Product
configuration UI, or connector-specific business policy.

The machine-readable authority is
`governance/connector-package-spec-v0.1.schema.json`. The connector catalog
in `governance/connectors.yaml` is a static candidate projection and points to
the same schema.

## Identity and authority

`package.id` and `package.version` are the canonical PluginManifest `id` and
`version`. They are repeated in this descriptor only so a catalog consumer can
cross-check the manifest reference; they must never be independently edited.

`capability.id` describes what the implementation exposes, for example
`message.connector.v1`. `connector.family` and `connector.type` classify the
implementation. A configured capability instance is a separate Platform
binding and is not represented by this package identity.

The package descriptor is static metadata. Platform resolution pins its
artifact through the existing `PluginSetLock`; the descriptor does not create
a second registry or resolver.

## Required package data

- `implementation.artifact` is either `PUBLISHED` with a URI and a
  `sha256:<64-hex>` digest, or `NOT_PUBLISHED` for a candidate that has no
  honest artifact yet.
- `dependencies.lock` is either `LOCKED` with an exact lock-file reference,
  format, and digest, or `NOT_PUBLISHED`. Production packages may not mean
  `pip install latest` or an equivalent floating install.
- `configuration.schema_ref` identifies the configuration schema supplied by
  the implementation. Secret values are never stored in a manifest or this
  descriptor.
- `runtime.kind` uses the Platform vocabulary (`subprocess-python`,
  `subprocess-jvm`, or `service`). `runtime.external` declares a required
  external runtime without naming vendor/Product semantics.
- `integrity` carries artifact/archive digests and an optional signature
  reference. A `PUBLISHED` descriptor requires both digests.
- `lifecycle.rollback_identity` is the immutable package version identity;
  `lifecycle.cache_identity` is the content-addressed cache key. Neither is a
  worker PID or runtime generation.
- `compatibility` records the Platform and capability interface constraints.
- `provenance` leaves hooks for source revision, builder, SBOM, and attestation
  references. Missing SBOM/attestation data is represented as `null`, not as a
  false claim.

## Static catalog versus dynamic installation

The descriptor's `publication_status` is limited to `NOT_PUBLISHED`,
`CANDIDATE`, and `PUBLISHED`. It must not contain `INSTALLED`, `RUNNING`, or
`CONFIGURED` state. Those states belong to a node-local installation record,
which is the second record type in the schema and is owned by the existing
Python/C# PluginStore implementations.

The v0.1 installation record is intentionally small: package identity,
version, verified artifact digest, content-addressed cache identity, current
state, active version, rollback identity, and bounded version history. The
existing store already provides offline archive verification, safe extraction,
activation, rollback, and cache storage. A future store persistence change may
materialize this record without changing the static catalog contract.

Supported installation states are `STAGED`, `INSTALLED`, `ACTIVE`, `FAILED`,
`ROLLED_BACK`, and `REMOVED`. Runtime liveness and worker generation remain
observations of the active runtime; they are not package identity.

## Candidate first connector

The OneBot v11 catalog entry is a `CANDIDATE` package descriptor. It records
the generic `message.connector.v1` capability, the `aiocqhttp` implementation
type, external protocol-runtime separation, and the configuration source, but
does not invent a package artifact, dependency lock digest, signature, SBOM,
or installation state. Task 4 may promote a descriptor only after an actual
immutable artifact and exact dependency lock are produced.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene Connector 包规范 v0.1

## 目的

这是一个可替换 Connector 的最小包合约；Connector 通过现有 Platform `PluginManifest`、`PluginSetLock` 和节点本地 `PluginStore` 接口解析。本文说明如何提供并验证 Connector；不定义市场、Product 配置 UI 或 Connector 专属业务策略。

机器可读的权威定义位于 `governance/connector-package-spec-v0.1.schema.json`。`governance/connectors.yaml` 中的 Connector 目录是静态候选投影，并引用同一份 schema。

## 身份与权威

`package.id` 和 `package.version` 分别是规范 `PluginManifest` 的 `id` 和 `version`。它们在此描述符中重复出现，只为目录消费者交叉核对 manifest 引用；绝不能单独修改。

`capability.id` 描述实现对外提供的能力，例如 `message.connector.v1`。`connector.family` 和 `connector.type` 用于分类实现。已配置的能力实例属于单独的 Platform binding，不由此包身份表示。

包描述符是静态元数据。Platform 通过现有 `PluginSetLock` 固定其制品；描述符不会创建第二个 registry 或 resolver。

## 必需的包数据

- `implementation.artifact` 必须是带 URI 和 `sha256:<64-hex>` 摘要的 `PUBLISHED`，或者是在尚无真实制品时使用的 `NOT_PUBLISHED`。
- `dependencies.lock` 必须是带有精确锁文件引用、格式和摘要的 `LOCKED`，或者是 `NOT_PUBLISHED`。生产包不得使用 `pip install latest` 或等效的浮动版本安装。
- `configuration.schema_ref` 标识实现提供的配置 schema。manifest 或此描述符中绝不保存密钥值。
- `runtime.kind` 使用 Platform 词汇（`subprocess-python`、`subprocess-jvm` 或 `service`）。`runtime.external` 声明所需的外部运行时，但不引入厂商 / Product 语义。
- `integrity` 包含制品 / 归档摘要和可选签名引用。`PUBLISHED` 描述符必须同时提供两种摘要。
- `lifecycle.rollback_identity` 是不可变的包版本身份；`lifecycle.cache_identity` 是内容寻址的缓存键。两者都不能是 Worker PID 或运行时代次。
- `compatibility` 记录 Platform 和能力接口约束。
- `provenance` 为源码修订版、构建器、SBOM 和证明材料引用预留位置。缺少 SBOM / 证明材料时应表示为 `null`，不得做出虚假声明。

## 静态目录与动态安装

描述符的 `publication_status` 仅限于 `NOT_PUBLISHED`、`CANDIDATE` 和 `PUBLISHED`。不得在其中包含 `INSTALLED`、`RUNNING` 或 `CONFIGURED` 状态。这些状态属于节点本地安装记录；该记录是 schema 中的第二种记录类型，由现有 Python/C# `PluginStore` 实现负责。

v0.1 安装记录刻意保持精简，只包含包身份、版本、已验证的制品摘要、内容寻址缓存身份、当前状态、活动版本、回滚身份和有限长度的版本历史。现有 Store 已支持离线归档校验、安全解压、激活、回滚和缓存存储。未来可以调整 Store 持久化以生成这份记录，而不改变静态目录合约。

支持的安装状态为 `STAGED`、`INSTALLED`、`ACTIVE`、`FAILED`、`ROLLED_BACK` 和 `REMOVED`。运行时存活情况和 Worker 代次仍是对活动运行时的观测值，不属于包身份。

## 首个候选 Connector

OneBot v11 目录条目是一个 `CANDIDATE` 包描述符。它记录通用的 `message.connector.v1` 能力、`aiocqhttp` 实现类型、外部协议运行时隔离方式和配置来源，但不会虚构包制品、依赖锁摘要、签名、SBOM 或安装状态。只有实际生成不可变制品和精确依赖锁后，任务 4 才能将该描述符提升为已发布状态。
