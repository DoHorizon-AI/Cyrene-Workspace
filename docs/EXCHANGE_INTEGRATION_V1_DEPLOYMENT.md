# Exchange Integration v1 Deployment and Acceptance

This guide implements the deployment side of the [Exchange Integration v1 ADR](ADR_EXCHANGE_INTEGRATION_V1.md). It describes the Workspace installer and host configuration; it does not claim that native provider acceptance has run.

## Modes and service lifecycle

The installed Exchange API defaults to `gateway-only`. Its existing gateway routes and database remain available without an integration profile. `integration-only` enables the integration API without model gateway routes. `combined` enables both route groups. The dispatcher runs as a separate systemd service from the same immutable Exchange release.

| `CYRENE_EXCHANGE_RUNTIME_MODE` | Exchange API | Dispatcher | Model gateway |
| --- | --- | --- | --- |
| `gateway-only` (default) | Existing gateway routes | Skipped by its systemd mode guard | Available |
| `integration-only` | Integration routes | Separate service process | Routes are absent |
| `combined` | Gateway and integration routes | Separate service process | Available |

The API unit uses `Wants=` for the dispatcher, so dispatcher startup failure does not prevent the API from starting. The two units can be stopped independently. Package Runtime updates for the Exchange component manage the primary API unit and its catalog-declared auxiliary dispatcher unit together. With valid integration configuration, a disconnected Plugin/provider affects integration health and does not fail gateway readiness. Invalid integration configuration is rejected during preflight; there is no silent fallback. In `combined` mode it also prevents that API process from starting, while `gateway-only` does not load integration configuration.

The default package configuration is in `/etc/cyrene/exchange-integration.env`:

```ini
CYRENE_EXCHANGE_RUNTIME_MODE=gateway-only
CYRENE_EXCHANGE_INTEGRATION_DATABASE=/var/lib/cyrene/exchange/integrations.sqlite3
```

The existing gateway database is `/var/lib/cyrene/exchange/exchange.sqlite3`. Keep the integration database at its separate path. Combined mode rejects a path collision.

To change modes, update `CYRENE_EXCHANGE_RUNTIME_MODE` and restart both units so systemd reevaluates the dispatcher condition:

```bash
sudo systemctl restart cyrene-exchange.service
sudo systemctl restart cyrene-exchange-integration-dispatcher.service
```

The dispatcher condition exits successfully only for `integration-only` and `combined`; `gateway-only` skips the unit. An invalid mode fails the dispatcher unit visibly.

## Selecting managed Plugin packages

The optional integration workload uses the existing Workspace v2 catalog and Platform Package Runtime lifecycle. The selector accepts the canonical package IDs from the Plugin manifests and maps each through the catalog before submitting the ordinary digest-bound workload plan:

```bash
sudo cyrene workload install exchange-integration --channel preview \
  --selected-provider-plugin cyrene.accounts.account-center \
  --selected-provider-plugin cyrene.providers.github-issues \
  --selected-provider-plugin cyrene.connectors.wecom
```

The currently published Integration v1 package tags are immutable `preview`
releases; `--channel preview` selects them without changing the catalog's
default channel.

The workload requires all three packages for this fixed vertical. Account Center remains the owner of linked accounts and credentials; Exchange receives only Platform-resolved connection facts and opaque account references. WeCom's package manifest retains both `message.connector.v1` and `tool.provider.v1` capabilities; the catalog's primary capability field does not replace the manifest's full capability list.

Before the Package Runtime `check` operation, Workspace invokes the active immutable Exchange bundle's `integrations check-config` path. The check parses the closed configuration, matches the `exchange-prod-v1` Platform workload profile, checks its public certificate pins and host secret references, and requires the fixed Account Center, GitHub Issues, and WeCom bindings plus at least one configured inbound WeCom source. It opens no listener, database, or network RPC. A failed check exits before creating a workload plan or writing installation state.

The Package Runtime plan remains the authority for release discovery, publisher verification, package installation, and activation. The Exchange API and dispatcher use the same active immutable Product bundle; no Exchange-owned plugin manager is introduced.

## Host configuration and secret references

`/etc/cyrene/exchange-integration.env` holds non-secret Exchange configuration. Add the closed Exchange JSON there when integrations are enabled. The Platform product workload profile remains in Platform-owned `/etc/cyrene/workspace-authority.env` under `CYRENE_AUTHORITY_PRODUCT_WORKLOAD_PROFILES_JSON`; do not copy it into the Exchange file. The profile matches `workloadId` `exchange-prod-v1`, owner `exchange`, target `cy-workspace-exchange`, and audience `exchange`. Its certificate fingerprints are public SHA-256 pins; the certificate private key remains host-managed. Before planning, Workspace reads only this profile variable from the Platform file and passes it to Exchange's local config checker; the Authority service remains the runtime owner of the profile.

The Exchange JSON configuration is supplied through `CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON`. It has `schemaVersion: 1`, a `hostSecretRefs` map from stable reference IDs to uppercase environment variable names, a `platformAuthority.productWorkload` mTLS profile, and fixed `accountCenter`, `githubIssues`, and `wecom` connection bindings. The `wecom.inboundSources` map is keyed by stable binding UUID; each source supplies its organization/workspace, source authority, bot namespace, account, group conversation, and a dedicated request HMAC secret reference. Exchange rejects unknown fields, duplicate JSON keys, unused secret references, and missing inbound sources.

The host-managed `/etc/cyrene/exchange-integration-secrets.env` is loaded by both Exchange units. Keep it outside source control, root-owned, and mode `0600`. Its variable names must match `hostSecretRefs`; HMAC values and TLS certificate/key paths are supplied by the host secret manager. Neither the JSON configuration nor the installer stores those values. TLS private-key files must have owner-only permissions, and Product workload certificates must match the configured Platform SHA-256 pins.

Each Plugin binding receives its own `CYRENE_CAPABILITY_CONFIGURATION_JSON`. Its `hostSecretRefs` maps stable references to environment variable names and contains no secret values. Platform Package Runtime supplies Account Center's persistent private location through the reserved `CYRENE_PLUGIN_STORAGE_PATH` environment value, derived from the validated binding and installed package identity. The path is mode `0700` and persists across package upgrades, deactivation, and uninstall. Account Center stores its private SQLite ledger there; Exchange data remains in the separate Exchange integration database.

Use the closed schemas owned by Exchange and the Plugin manifests as the configuration source of truth. Do not copy their schemas into Workspace or replace connection references with source-checkout paths.

## Acceptance evidence

The following environment gates remain **NOT_RUN** until their named host or tenant evidence is recorded. Local source tests and mocked transports do not satisfy these gates.

| Gate | Status | Required evidence |
| --- | --- | --- |
| Platform Product workload mTLS | **NOT_RUN** | Installed managed certificate profile, live certificate fingerprint match, and authenticated Product workload validation on the native host |
| GitHub application and user-scoped access | **NOT_RUN** | Real GitHub application/OAuth setup, authorized actor ACL, and verified issue read/write read-back |
| Official WeCom bot | **NOT_RUN** | Real tenant bot connection, signed inbound challenge/source verification, and official SDK send acknowledgement |
| Native API/dispatcher lifecycle | **NOT_RUN** | Native host boot, mode changes, isolated dispatcher failure, and persisted recovery across restart |
| PostgreSQL deployment | **NOT_RUN** | No PostgreSQL adapter is part of the SQLite MVP; PostgreSQL migration, concurrency, backup, and recovery evidence remain future work |
| Real provider end-to-end flow | **NOT_RUN** | Signed GitHub event, persisted Exchange inbox, Account Center actor binding, Platform delegation, WeCom acknowledgement, and zero model calls on real authorized tenants |

<!-- Chinese Translation / 中文翻译 -->

# Exchange Integration v1 部署与验收

本指南落实 [Exchange Integration v1 ADR](ADR_EXCHANGE_INTEGRATION_V1.md) 的部署部分，说明 Workspace 安装器和主机配置；它不代表原生 Provider 验收已经执行。

## 模式与服务生命周期

已安装的 Exchange API 默认运行于 `gateway-only`。没有集成配置时，已有 gateway 路由和数据库仍可用。`integration-only` 仅启用集成 API，不提供 model gateway 路由。`combined` 同时启用两组路由。Dispatcher 由独立 systemd 服务运行，使用同一个不可变 Exchange 发布包。

| `CYRENE_EXCHANGE_RUNTIME_MODE` | Exchange API | Dispatcher | Model gateway |
| --- | --- | --- | --- |
| `gateway-only`（默认） | 已有 gateway 路由 | 被 systemd 模式条件跳过 | 可用 |
| `integration-only` | 集成路由 | 独立服务进程 | 不提供路由 |
| `combined` | Gateway 与集成路由 | 独立服务进程 | 可用 |

API 单元使用 `Wants=` 启动 Dispatcher；Dispatcher 启动失败不会阻止 API 启动。两个单元可以分别停止。Platform Package Runtime 更新 Exchange 组件时，会通过目录声明的辅助单元同时管理 API 和 Dispatcher。集成配置有效时，Plugin/Provider 断连会影响集成健康状态，但不会使 gateway 就绪失败。无效集成配置会在安装前置检查中被拒绝，不会静默降级；`combined` 模式下它也会阻止该 API 进程启动，而 `gateway-only` 不会加载集成配置。

默认配置位于 `/etc/cyrene/exchange-integration.env`：

```ini
CYRENE_EXCHANGE_RUNTIME_MODE=gateway-only
CYRENE_EXCHANGE_INTEGRATION_DATABASE=/var/lib/cyrene/exchange/integrations.sqlite3
```

已有 gateway 数据库为 `/var/lib/cyrene/exchange/exchange.sqlite3`。集成数据库保持独立路径；`combined` 模式会拒绝两个数据库路径冲突。

修改模式后更新 `CYRENE_EXCHANGE_RUNTIME_MODE`，并重启两个单元，让 systemd 重新计算 Dispatcher 条件：

```bash
sudo systemctl restart cyrene-exchange.service
sudo systemctl restart cyrene-exchange-integration-dispatcher.service
```

Dispatcher 条件仅在 `integration-only` 和 `combined` 下通过；`gateway-only` 会跳过该单元。无效模式会让 Dispatcher 单元明确失败。

## 选择受管 Plugin 包

可选集成工作负载使用现有 Workspace v2 目录和 Platform Package Runtime 生命周期。选择器接受 Plugin manifest 中的规范 package ID，再通过目录映射后提交普通的摘要绑定工作负载计划：

```bash
sudo cyrene workload install exchange-integration --channel preview \
  --selected-provider-plugin cyrene.accounts.account-center \
  --selected-provider-plugin cyrene.providers.github-issues \
  --selected-provider-plugin cyrene.connectors.wecom
```

当前已发布的 Integration v1 package tag 是不可变 `preview` release；`--channel preview`
只用于选择这些包，不会更改目录的默认 channel。

此固定纵向流程需要全部三个包。Account Center 继续拥有外部账户链接和凭据；Exchange 只接收由 Platform 解析的连接事实和不透明账户引用。WeCom package manifest 保留 `message.connector.v1` 与 `tool.provider.v1` 两项能力；目录中的 primary capability 字段不会覆盖 manifest 的完整能力列表。

在 Package Runtime 执行 `check` 之前，Workspace 会调用活动不可变 Exchange 包的 `integrations check-config`。它解析封闭配置、匹配 `exchange-prod-v1` Platform workload profile、检查公开证书 pin 和主机 secret reference，并要求固定的 Account Center、GitHub Issues、WeCom 绑定以及至少一个 WeCom 入站来源。该检查不会启动监听器、创建数据库或发起网络 RPC。检查失败会在创建工作负载计划或写入安装状态之前退出。

Package Runtime 计划仍负责 release discovery、publisher 验证、Plugin 包安装和激活。Exchange API 与 Dispatcher 使用同一个活动不可变 Product 包；不会引入 Exchange 自有插件管理器。

## 主机配置与 secret reference

`/etc/cyrene/exchange-integration.env` 保存 Exchange 非敏感配置。启用集成时，在其中提供 Exchange 封闭 JSON 配置。Platform Product workload profile 由 Platform 管理，保存在 `/etc/cyrene/workspace-authority.env` 的 `CYRENE_AUTHORITY_PRODUCT_WORKLOAD_PROFILES_JSON` 中；不要复制到 Exchange 文件。该 profile 匹配 `workloadId` `exchange-prod-v1`、owner `exchange`、target `cy-workspace-exchange` 和 audience `exchange`。证书指纹是公开 SHA-256 pin；证书私钥由主机管理。生成计划前，Workspace 只从 Platform 文件读取这个 profile 变量并交给 Exchange 本地配置检查器；运行时仍由 Authority 服务负责此 profile。

Exchange JSON 配置通过 `CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON` 提供，包含 `schemaVersion: 1`、从稳定引用 ID 到大写环境变量名的 `hostSecretRefs`、`platformAuthority.productWorkload` mTLS profile，以及固定的 `accountCenter`、`githubIssues` 和 `wecom` 连接绑定。`wecom.inboundSources` 以稳定 binding UUID 为键；每个来源包含组织/工作区、来源 authority、bot namespace、账户、群聊 conversation 和独立的请求 HMAC secret reference。Exchange 会拒绝未知字段、重复 JSON key、未使用的 secret reference 和缺少的入站来源。

主机管理的 `/etc/cyrene/exchange-integration-secrets.env` 由两个 Exchange 单元加载。该文件不得进入源代码库，应由 root 所有且权限为 `0600`。变量名必须与 `hostSecretRefs` 匹配；HMAC 值以及 TLS 证书/密钥路径由主机 secret manager 提供。JSON 配置和安装器都不保存这些值。TLS 私钥文件必须只有 owner 可读，Product workload 证书必须与配置的 Platform SHA-256 pin 匹配。

每个 Plugin binding 都有自己的 `CYRENE_CAPABILITY_CONFIGURATION_JSON`。其中 `hostSecretRefs` 只保存稳定引用到环境变量名的映射，不包含 secret 值。Platform Package Runtime 会通过保留环境变量 `CYRENE_PLUGIN_STORAGE_PATH` 提供 Account Center 私有持久化位置；路径根据已验证的 binding 和已安装 package 身份派生，权限为 `0700`，并在包升级、停用和卸载后保留。Account Center 在此存放私有 SQLite ledger；Exchange 数据仍保存在独立的 Exchange 集成数据库中。

配置以 Exchange 和 Plugin manifest 所有的封闭 schema 为准。不要把这些 schema 复制到 Workspace，也不要用源码检出路径替代连接引用。

## 验收证据

以下环境门禁保持 **NOT_RUN**，直到记录对应主机或租户证据。源码测试和 mock transport 不能替代这些门禁。

| 门禁 | 状态 | 所需证据 |
| --- | --- | --- |
| Platform Product workload mTLS | **NOT_RUN** | 原生主机已安装受管证书 profile，实时证书指纹匹配，并通过已认证 Product workload 校验 |
| GitHub 应用与用户级访问 | **NOT_RUN** | 真实 GitHub application/OAuth 配置、授权 actor ACL 和已读回验证的 issue 读写结果 |
| 企业微信官方 Bot | **NOT_RUN** | 真实租户 Bot 连接、已签名的入站 challenge/来源验证和官方 SDK 发送 ACK |
| 原生 API/Dispatcher 生命周期 | **NOT_RUN** | 原生主机启动、模式切换、Dispatcher 单独故障，以及重启后的持久化恢复 |
| PostgreSQL 部署 | **NOT_RUN** | SQLite MVP 未包含 PostgreSQL adapter；PostgreSQL 迁移、并发、备份和恢复证据属于后续工作 |
| 真实 Provider 端到端流程 | **NOT_RUN** | 真实授权租户上的 GitHub 签名事件、Exchange 持久 inbox、Account Center actor 绑定、Platform delegation、WeCom ACK 和零模型调用 |
