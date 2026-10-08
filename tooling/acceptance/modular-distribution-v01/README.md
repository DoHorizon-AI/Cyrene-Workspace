# Modular distribution clean-host acceptance / Modular Distribution 干净宿主验收

This acceptance flow uses a new `ubuntu-24.04` GitHub-hosted VM and immutable
official release assets. It does not check out or clone Workspace, Product,
Platform, Plugins, or Client source. The workflow input pins are assertions
against signed installer and resolver identities; the installed DEB, its bound
catalog, and the installed workload helper remain the runtime authorities.

本验收使用全新的 `ubuntu-24.04` GitHub 托管虚拟机和官方不可变发布物，不 checkout 或 clone
Workspace、Product、Platform、Plugins 或 Client 源码。工作流输入的 pins 只用于断言已签名安装包与
解析器给出的身份；运行时权威仍是已验证的 DEB、其绑定的 catalog 和安装后的 workload helper。

## Dispatch inputs / 手动触发输入

Run `.github/workflows/modular-distribution-acceptance.yml` with:

- `native_release_id`: the exact official `native-installer-preview-<40 lowercase hex>` tag.
- `release_pins_json`: a JSON document matching `release-pins-v1.schema.json`. It must pin the same native tag, Workspace source SHA and workflow, Ubuntu 24.04 amd64 DEB asset digest/size, v2 catalog release identity/digest, Catalyst catalog digest, and exact release identities for Catalyst, Node 24 Client Control, the Client Web host, Runtime Maintenance SDK, and all four Platform-supervised data plugins. The schema also accepts an optional exact Echo OCI, Runtime SDK, and `cyrene-evaluation-exact-match` identity set; the current driver refuses Echo pins before native installation until Workspace publishes the supported OCI install/evaluate/uninstall path. The workflow rejects incomplete Catalyst pins before native installation. It verifies the attested source receipt before install, then reads back dpkg state and the DEB-installed catalogs, active v2 binding, and static stage-only package contract; it does not expect a per-host native transaction receipt.

This workflow always runs the pinned Catalyst core, a non-mutating resolver
check that proves a required component cannot be omitted, and a read-only
version-conflict inspection of the exact signed Catalyst resolution. The latter
confirms the intended plan has no `VERSION_CONFLICT` blocker; it does not claim to
exercise rejection of a conflicting Catalog fixture. The Catalyst plan must select
all four Catalog-owned plugin bindings. Acceptance compares Catalog
binding identities, Package Runtime source policy, ActivitySource Broker scopes,
and authenticated Platform UDS readback, including each active installation ID.
The exact Node 24 Control release, root-owned active pointer/receipt, systemd
unit, local auth session, and Catalyst read proxy are required core gates.
An isolated stage retry is exercised with the installed CLI in a temporary Linux
network namespace. Only that stage child process loses network access; the host
network, updater Unix sockets, and installed data are unchanged. The driver requires
a retryable `NETWORK_ERROR`, verifies that no selected component became active,
retries the same `planId`/`planDigest` online, then repeats stage and compares the
staged identities. If the isolated stage succeeds from already available content,
the ledger records cache-backed idempotence and leaves failure recovery `NOT_RUN`;
it never reports a cached success as failure recovery. A separately pinned
exact-match plugin can become the fallback exercise once Echo installation is
supported.

Echo exact-match/evaluate/uninstall and whole-VM reboot recovery remain explicit
`NOT_RUN` gates until their safe execution contracts and exact pins are supplied.
Interrupted installer-transaction recovery also remains `NOT_RUN`: the current
installer does not publish a deferred-transaction fault-injection/resume API, and
acceptance never edits maintenance journals. The existing Catalyst systemd restart
test covers service startup recovery only. Platform-supervised activation
of all four selected Plugins is a required core gate, not an optional phase.
They remain visible in the phase ledger and keep full-distribution acceptance
incomplete; they are not inferred from container or source-checkout evidence.

The Node 24 Client Control and static Web current pointers, immutable receipts,
and manifest-pinned file hashes are checked as part of workload receipt readback.
The required HTTP gate
then checks the installed index bytes at `http://127.0.0.1:8100/` against the
verified receipt, the managed `cyrene-workspace-web.service` status, `/healthz`,
direct Studio Control readiness at `127.0.0.1:5182/health/ready`, and the
same-origin `/api/v1/auth/session` local-auth/CSRF contract. It validates the
CSRF token in memory and discards it. The separate Client integration gate
verifies the exact Control unit against its signed archive and compares the
direct Catalyst dataset response with the same-origin read-only proxy's JSON
schema; it stores only response hashes and lengths, never dataset records or
credentials. It also reads the installed `/etc/cyrene/studio-control.env`
inside a root-only probe, requires the pinned loopback Catalyst origin and its
protected token-file reference, and verifies the signed unit's fixed
`STUDIO_PUBLIC_ORIGINS`. The probe never returns either token-file path contents
or token bytes.

该工作流始终运行带精确 pins 的 Catalyst core，并进行只读 resolver 检查，验证必需组件不能被排除及准确计划没有
`VERSION_CONFLICT` blocker。此检查不声称测试了冲突配置的拒绝行为。离线 stage 重试只在一个 Linux network namespace
隔离的 CLI 子进程中执行；不会改变宿主网络、更新器 Unix socket
或已安装数据。验收要求失败为可重试的 `NETWORK_ERROR`、selected component 未激活，再用同一个 `planId` 和
`planDigest` 在线重试并重复 stage 比对身份。如果离线 stage 因本地已有内容而成功，ledger 只记录缓存幂等性，恢复
失败仍为 `NOT_RUN`，不会把缓存成功称作故障恢复。Echo 安装就绪后可用其精确 pin 的 exact-match 插件做备用断网用例。

Echo exact-match/evaluate/uninstall、整机重启和中断 installer transaction 恢复仍保持显式 `NOT_RUN`。当前 installer
没有正式 deferred-transaction fault-injection/resume API，验收不会编辑 maintenance journal；已有 Catalyst systemd
restart 只证明服务启动恢复，不冒充安装事务回滚。相关阶段会显示在 phase ledger 中，不会从容器或源码 checkout
证据推断通过。

验收还会通过 root-only probe 读取已安装的 `/etc/cyrene/studio-control.env`，要求 Catalyst loopback URL 与 pins 一致，token 文件引用受保护，并确认签名 unit 固定 `STUDIO_PUBLIC_ORIGINS`。证据不包含环境文件值、token 路径内容或 token 字节。

Platform 监管的四个插件 activation 属于必需 core gate。验收同时对照已签名 Catalog、Package Runtime source policy、
ActivitySource mutation scopes 和经认证的 Platform UDS 状态，并要求每个所选安装处于 `RUNNING`。

`release-pins-v1.schema.json` constrains the root-supplied expected identities. Pins do
not choose a “latest” release and do not provide artifact bytes or runtime state.
The resolver's `check` response must select exactly the pinned component set and
preserve each pinned target, version, release tag, manifest digest, artifact digest,
publisher workflow, index digest, and attestation source commit before `stage` or
`apply` can run.

`release-pins-v1.schema.json` 约束 root 提供的预期身份。pins 不选择“latest”版本，也不提供制品字节或运行时状态。
`check` 必须在 `stage` 或 `apply` 前返回与 pins 完全相同的组件集合、target、版本、release tag、manifest/artifact
摘要、publisher workflow、index 摘要和 attestation source commit。

## Evidence and phase semantics / 证据与阶段状态

The job records its runner image, OS, architecture, PID 1 and systemd facts; the
exact release tag and source identity; native SLSA verification results; downloaded
asset hashes and sizes; the selected workload plan and its plan digest; dpkg and
installed catalog/binding/status readback; exact active and immutable workload receipts,
the static Web pointer/tree identity and live HTTP routes; ActivitySource/token/generation
and selected binding activation readback; Catalyst active bundle identity and receipt
linkage; Catalyst HTTP liveness; producer and independent consumer results; and service
restart recovery. The job artifact
contains these records and command logs, but not the generated SFT ZIP or its
contents. Raw Catalyst producer/consumer stdout and stderr are withheld; only their
byte counts and SHA-256 values are recorded. `GH_TOKEN` is scoped to release download, attestation verification,
and the installed resolver's check/stage requests. It has read-only repository
and attestation permissions; apply, repeat-install, and Product invocations run
without that token. The isolated network-failure attempt withholds raw stdout/stderr
and persists only the error code and retryable flag.

`GH_TOKEN` 仅授予仓库与 attestation 只读权限，用于发布物下载、验签以及已安装 resolver 的 check/stage；apply、重复安装与 Product 调用不会继承该 token。

Each phase is recorded as `PASS`, `FAIL`, or `NOT_RUN` with a reason. The
ledger reports the Catalyst required core and Phase 2 safety/reliability/optional gates
separately, including their own incomplete-phase lists. The
`version_conflict_gate` is a read-only no-conflict assertion over the exact
selected plan. `offline_retry_gate` is a recovered-failure pass only when the
isolated stage returns retryable `NETWORK_ERROR` and the same plan later stages
successfully. A cached isolated-stage success is recorded separately and cannot
make that recovery gate pass. `installer_interrupted_transaction_recovery` stays
`NOT_RUN` until the installer exposes a supported host-fault/recovery contract.
A failed precondition stops later mutations; the finalizer records all unreached phases as
`NOT_RUN`. Missing commands, receipts, signed assets, service activation, or API
capabilities are failures for the required phase and cannot produce an overall
acceptance pass. Whole-VM reboot recovery is `NOT_RUN` on a disposable hosted runner;
the tested systemd service restart is recorded separately and is not presented as
a whole-host reboot.

`version_conflict_gate` 只读取已签名精确计划并断言没有 `VERSION_CONFLICT` blocker，不伪称拒绝测试。
`offline_retry_gate` 只有在隔离 stage 返回可重试 `NETWORK_ERROR`、相同计划在线恢复并重复 stage 身份一致时才为 `PASS`；
缓存导致的隔离成功会单独记录，不会让恢复 gate 通过。`installer_interrupted_transaction_recovery` 在正式宿主故障与恢复
接口发布前保持 `NOT_RUN`。phase ledger 单独汇总 Catalyst 必需 core 与 Phase 2 safety/reliability/optional gates 及其未通过阶段。

每阶段记录 `PASS`、`FAIL` 或 `NOT_RUN` 及原因。前置条件失败会阻止后续变更；收尾器会将未到达阶段记为
`NOT_RUN`。缺少命令、receipt、签名发布物、服务激活或 API capability 会使对应必需阶段失败，不能形成总体验收
通过。一次性托管 runner 不执行整机重启，因此整机重启恢复为 `NOT_RUN`；systemd 服务重启单独记录，不冒充整机重启。

The required Catalyst path is:

1. Verify the exact immutable native release, its SLSA source identity and detached bundle, catalog bytes/bundle, DEB metadata size and SHA-256, then install only that local DEB with `apt`.
2. Read back dpkg status, the root-owned baseline and active catalogs, the installed signed v2 binding through the DEB-installed binding validator, and the stage-only package contract. Query the fixed workload status API. Check the exact Catalyst plan against pins; stage and apply only that digest-bound plan; read back each root-owned active and immutable workload receipt through the installed manifest validator. Verify the Client static-Web current pointer, release tree, and manifest-pinned file hashes.
3. Read back the ActivitySource catalog, Package Runtime source policy, exact Catalyst `cyrene` UID/GID, selected binding scopes, root-only token mode and hash match without exporting the token, shared catalog generation, and the running systemd process' loaded source/generation/socket identity. Compare each selected binding/package/installation ID with authenticated Platform UDS readback and require its active installation to be `RUNNING`. Require the Product's loaded port to match the pinned loopback URL. The workflow does not perform `init-catalog`; these scopes must be created by the installed workload transaction.
4. Validate the installed Catalyst immutable bundle with the installed Workspace service-bundle validator and compare its active/immutable receipt release path, pointer and bundle identities plus signed Product source/target/digests with resolver pins. Check `/healthz`, then run the installed `cyrene_catalyst.external_acceptance` module and independently verify its ZIP with `cyrene_catalyst_consumer.training_bundle` from the same active bundle. No source checkout or development venv may supply modules or fixtures.
5. Verify the installed Web host metadata and live HTTP routes against the exact Client receipt; restart `cyrene-catalyst.service`, wait for systemd and HTTP recovery, then repeat the public `sudo cyrene workload install catalyst --yes` command to prove idempotence and read back the same exact installed identities.
6. Submit a resolver check that excludes one required Catalyst component; require the expected blocked response and prove that no staging or apply request was sent.

Catalyst 已安装验收模块当前由活动 immutable service bundle 提供；runner 使用固定私有解释器
`/opt/cyrene/python/3.12.14/bin/python3.12` 与活动 bundle 的 `python/` 路径。服务端口默认
`127.0.0.1:8004`；最终命令必须与 rootpins 和已安装 Product listener 配置一致。
`/healthz` 只作为存活检查，producer/consumer 的正式 API 请求才是实际使用证据。

The workflow is deliberately strict: it fails when an expected API or installer
operation is not implemented. Dispatch requires the exact native tag, v2 catalog
binding, and exact Catalyst, Platform-supervised Plugin, Runtime SDK, and Client
release pins. Optional Echo identities are schema-validated but are rejected before
native installation until the signed OCI installer path and installed evaluator
contract are executable. Additional Echo, interrupted-transaction, and whole-host
reboot gates remain required for a full-distribution pass. Do not substitute the earlier first-product
cohort receipt, a source checkout, a privileged container, or a different native
tag to make a phase pass.

工作流遇到未实现的 API 或安装操作会严格失败。必须等 exact native tag、v2 catalog binding、Catalyst、四个受监管插件、
Runtime SDK 和 Client 的官方发布 pins 都准备好后，才能运行验收。可选 Echo pins 必须精确包含官方 OCI Product、Runtime
SDK 和 exact-match plugin；当前驱动会在任何 native 安装前拒绝这些 pins，直到签名 OCI 安装及已安装评估/卸载流程具备
可执行契约。完整发行验收仍要求 Echo、事务中断恢复和整机重启 gate。
不得用旧 first-product cohort receipt、源码 checkout、privileged 容器或其他 native tag 来替代并制造 PASS。
