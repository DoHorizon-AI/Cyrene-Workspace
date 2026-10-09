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
- `release_pins_json`: a JSON document matching `release-pins-v1.schema.json`. Each workload pin must state `channel: "preview"`, and each component's index identity must name that same signed channel. It must pin the same native tag, Workspace source SHA and workflow, Ubuntu 24.04 amd64 DEB asset digest/size, v2 catalog release identity/digest, Catalyst catalog digest, and exact release identities for Catalyst, Node 24 Client Control, the Client Web host, Runtime Maintenance SDK, and all four Platform-supervised data plugins. Optional `workloads.plugins` pins contain exactly the official `cyrene-evaluation-exact-match` and Runtime SDK identities. They enable an independent stage-only offline retry if Catalyst's isolated stage is fully cache-backed and do not depend on Echo OCI. Optional `workloads.echo` pins contain the official Echo OCI image, Runtime SDK, and exact-match plugin identities. When present, the workflow runs a separate installed Echo check/stage/apply, exact-match Product API evaluation, and supported uninstall lifecycle after Catalyst core acceptance. These pins remain assertions against official signed resolver identities; they do not install Echo from source or synthesize a Plugin connection reference. The workflow rejects incomplete Catalyst pins before native installation. It verifies the attested source receipt before install, then reads back dpkg state and the DEB-installed catalogs, active v2 binding, and static stage-only package contract; it does not expect a per-host native transaction receipt.

The disposable runner must use GitHub CLI `gh` version 2.102.0 or newer for strict attestation verification. Before installing Cyrene, the driver records the initial CLI version; when the runner image is older, it reads GitHub's official release API and requires immutable `cli/cli` tag `v2.102.0` at commit `fc4b137cdef0a6bd28fd461b7cf9c84a5812a8cd`. It downloads the official amd64 DEB and checksum list, verifies their API asset digest/size, checksum-list SHA-256, exact DEB SHA-256, package version, and architecture, then installs that local verified DEB. The provision record captures the old/new versions and release asset identity. The workflow does not modify a developer's shared WSL CLI. Detached release and catalog attestations are still checked with exact signer, source ref, source SHA, predicate, and OIDC issuer constraints.

临时 runner 的 GitHub CLI `gh` 必须为 2.102.0 或更新版本，才能进行严格 provenance 验签。安装 Cyrene 前，driver 会记录 runner 原有版本；若镜像版本较旧，则读取 GitHub 官方 release API，并要求 `cli/cli` `v2.102.0` 为不可变发布且 source commit 精确为 `fc4b137cdef0a6bd28fd461b7cf9c84a5812a8cd`。driver 下载官方 amd64 DEB 与 checksum 文件，核对 API asset 摘要/大小、checksum 文件 SHA-256、DEB SHA-256、包版本和架构，再安装本地已验证的 DEB。provision 记录保留升级前后版本和发布资产身份；不会改动开发者共享 WSL 中的 CLI。发布物和 Catalog attestation 仍按精确 signer、source ref、source SHA、predicate 与 OIDC issuer 约束验签。

This workflow always runs the pinned Catalyst core, a non-mutating resolver
check that proves a required component cannot be omitted, and a read-only
version-conflict inspection of the exact signed Catalyst resolution. The latter
confirms the intended plan has no `VERSION_CONFLICT` blocker; it does not claim to
exercise rejection of a conflicting Catalog fixture. The Catalyst plan must select
all four Catalog-owned plugin bindings. Acceptance compares Catalog
binding identities, Package Runtime source policy, ActivitySource Broker scopes,
and authenticated Platform UDS readback, including each active installation ID.
The signed Catalog 15's default channel is `stable`, so this preview acceptance
explicitly requests `channel: "preview"` for the Catalyst and standalone plugin
checks. The workflow verifies `check.channel`, `resolution.channel`, and
`resolution.planDigestMaterial.channel`, then repeats the same channel on each
`stage` and `apply` request and checks their readbacks. Its public idempotence
command is `sudo cyrene workload install catalyst --channel preview --yes`.
Generic CLI installs that omit `--channel` continue to use the installed signed
Catalog's default. The pins require every selected release index to identify the
same preview channel.

签名 Catalog 15 的默认 channel 是 `stable`，因此此 preview 验收会在 Catalyst 和独立插件的
check 请求中显式传入 `channel: "preview"`。工作流核对 `check.channel`、`resolution.channel` 和
`resolution.planDigestMaterial.channel`，并在每次 `stage`、`apply` 请求中重复同一 channel，再验证响应。
幂等安装使用公开命令 `sudo cyrene workload install catalyst --channel preview --yes`。普通 CLI 命令省略
`--channel` 时仍按已安装签名 Catalog 的默认值运行。pins 要求每个所选 release index 都绑定同一个 preview channel。

The exact Node 24 Control release, root-owned active pointer/receipt, systemd
unit, local auth session, and Catalyst read proxy are required core gates.
An isolated stage retry is exercised with the installed CLI in a temporary Linux
network namespace. Only that stage child process loses network access; the host
network, updater Unix sockets, and installed data are unchanged. The driver requires
a retryable `NETWORK_ERROR`, verifies that no selected component became active,
retries the same `planId`/`planDigest` online, then repeats stage and compares the
staged identities. If the isolated stage succeeds from already available content,
the ledger records cache-backed idempotence and leaves failure recovery `NOT_RUN`;
it never reports a cached success as failure recovery. When exact `workloads.plugins`
pins are supplied, the driver checks a standalone plan selecting only
`cyrene-evaluation-exact-match` and its required Runtime SDK, then stages it in an
isolated namespace. A retryable network failure must leave installed identities
unchanged; the same plan is retried online and staged again for identity stability.
The fallback never applies the plan and does not require Echo OCI. If this plugin
stage is cache-backed too, the ledger records that fact and keeps failure recovery
`NOT_RUN`. If `workloads.plugins` pins are absent, the fallback is also `NOT_RUN`
with the missing exact identities recorded as the reason.

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
then checks the installed index bytes at both `http://127.0.0.1:8100/` and
`http://127.0.0.1:8100/datasets` against the verified receipt, the managed
`cyrene-workspace-web.service` status, `/healthz`,
direct Studio Control readiness at `127.0.0.1:5182/health/ready`, and the
same-origin `/api/v1/auth/session` local-auth/CSRF contract. It validates the
CSRF token in memory and discards it. The separate Client integration gate
verifies the exact Control unit against its signed archive and compares the
direct Catalyst dataset response with the same-origin read-only proxy's JSON
schema; it stores only response hashes and lengths, never dataset records or
credentials. After Catalyst's installed acceptance producer and independent
consumer pass, a required Client curation gate reuses that report's fixed
synthetic dataset and source revisions. It sends the published Client UI's
`curateTrainingData` request once through the same-origin proxy with the local
session's `X-CSRF-Token`, then reads the new processing run through the proxy and
direct Catalyst until both report `SUCCEEDED`. The evidence retains only safe
route/status facts, hashes, and source count. It never stores response rows,
CSRF material, or the server bearer. This adds one curation draft to the
synthetic acceptance dataset after the verified SFT export; it does not change
or regenerate the published SFT bundle. Browser interaction itself remains
`NOT_RUN`, since the workflow has no browser automation.

The gate also reads the installed `/etc/cyrene/studio-control.env`
inside a root-only probe, requires the pinned loopback Catalyst origin and its
protected token-file reference, and verifies the signed unit's fixed
`STUDIO_PUBLIC_ORIGINS`. The probe never returns either token-file path contents
or token bytes.

验收会对照已签名安装回执核验根路由和 `/datasets` 路由返回的 index 字节，并检查专属 Web 服务、health、Control readiness
以及同源本地 session/CSRF 契约。随后，在已安装 Catalyst 的真实验收生产者与独立 consumer 成功后，驱动复用报告中的固定合成
dataset 与 source revisions，通过同源代理只提交一次 Client UI 使用的 `curateTrainingData` 操作，并携带该 local session 的
`X-CSRF-Token`。驱动通过代理和 Catalyst 直连轮询新 run，要求两端最终都为 `SUCCEEDED`。证据只保留路由、状态、hash 和来源数，不
保存记录行、CSRF 凭据或服务端 bearer。该步骤会在合成验收 dataset 上增加一个 curation draft，但不会改变或重新生成已验证的
SFT 发布包；真实浏览器交互单独记录为 `NOT_RUN`。

该工作流始终运行带精确 pins 的 Catalyst core，并进行只读 resolver 检查，验证必需组件不能被排除及准确计划没有
`VERSION_CONFLICT` blocker。此检查不声称测试了冲突配置的拒绝行为。离线 stage 重试只在一个 Linux network namespace
隔离的 CLI 子进程中执行；不会改变宿主网络、更新器 Unix socket
或已安装数据。验收要求失败为可重试的 `NETWORK_ERROR`、selected component 未激活，再用同一个 `planId` 和
`planDigest` 在线重试并重复 stage 比对身份。如果离线 stage 因本地已有内容而成功，ledger 只记录缓存幂等性，恢复
失败仍为 `NOT_RUN`，不会把缓存成功称作故障恢复。如果 Catalyst 离线 stage 命中缓存且提供了 `workloads.plugins` 精确 pins，驱动会独立检查仅含 `cyrene-evaluation-exact-match` 与所需 Runtime SDK 的计划，在隔离子进程中 stage；遇到可重试网络错误后，验证已安装身份未改变，并用同一 plan 在线重试和重复 stage。该 fallback 不 apply，也不依赖 Echo OCI。如果此插件包也命中缓存，仍记为 `NOT_RUN` 并记录缓存结果。
没有提供 `workloads.plugins` pins 时，fallback 会记录缺少 exact identity，不会把 Echo OCI 是否可安装当作前置条件。

Echo exact-match/evaluate/uninstall、整机重启和中断 installer transaction 恢复仍保持显式 `NOT_RUN`。当前 installer
没有正式 deferred-transaction fault-injection/resume API，验收不会编辑 maintenance journal；已有 Catalyst systemd
restart 只证明服务启动恢复，不冒充安装事务回滚。相关阶段会显示在 phase ledger 中，不会从容器或源码 checkout
证据推断通过。

验收还会通过 root-only probe 读取已安装的 `/etc/cyrene/studio-control.env`，要求 Catalyst loopback URL 与 pins 一致，token 文件引用受保护，并确认签名 unit 固定 `STUDIO_PUBLIC_ORIGINS`。证据不包含环境文件值、token 路径内容或 token 字节。

Platform 监管的四个插件 activation 属于必需 core gate。验收同时对照已签名 Catalog、Package Runtime source policy、
ActivitySource mutation scopes 和经认证的 Platform UDS 状态，并要求每个所选安装处于 `RUNNING`。

`release-pins-v1.schema.json` constrains the root-supplied expected identities. Pins do
not choose a “latest” release and do not provide artifact bytes or runtime state.
Each workload separates direct roots in `selectedComponents` from dependency-only
identities in `dependencyComponents`. Before `stage` or `apply`, the driver derives
the direct selection and full transitive closure from the already verified signed
Catalog, compares every `closureReasons` edge with the resolver response, and
requires both pin arrays to partition that exact closure. The resolver's check and
stage rows must then match all pinned target, version, release tag, manifest and
artifact digests, publisher workflow, index digest, and attestation source identities.
Shared dependencies may have several distinct closure reasons but are pinned once;
build-only dependencies are excluded as they are by the resolver.

`release-pins-v1.schema.json` 约束 root 提供的预期身份。pins 不选择“latest”版本，也不提供制品字节或运行时状态。
每个 workload 将直接根组件放在 `selectedComponents`，将仅由依赖到达的身份放在 `dependencyComponents`。
`stage` 或 `apply` 前，驱动会根据已验签的 Catalog 推导直接选择和完整传递闭包，逐条对照 resolver 返回的
`closureReasons`，并要求两组 pins 恰好划分该闭包。随后要求 `check` 与 `stage` 行中的全部 pinned target、版本、
release tag、manifest/artifact 摘要、publisher workflow、index 摘要及 attestation source 身份完全一致。共享依赖可有多条
不同 closure reason，但只 pin 一次；与 resolver 一样，build-only 依赖不进入闭包。

## Evidence and phase semantics / 证据与阶段状态

The job records its runner image, OS, architecture, PID 1 and systemd facts; the
exact release tag and source identity; native SLSA verification results; downloaded
asset hashes and sizes; the selected workload plan and its plan digest; dpkg and
installed catalog/binding/status readback; exact active and immutable workload receipts,
the static Web pointer/tree identity and live `/` and `/datasets` routes; same-origin
Client curation mutation and direct Catalyst run readback; ActivitySource/token/generation
and selected binding activation readback; Catalyst active bundle identity and receipt
linkage; Catalyst HTTP liveness; producer and independent consumer results; and service
restart recovery. After the installed independent consumer succeeds, the job artifact
also contains `evidence/public-fixtures/authored-business-sft.zip`, the fixed synthetic
acceptance fixture with its independently checked SHA-256 and byte size. The workflow
does not copy that ZIP into uploadable evidence before consumer success. Raw Catalyst
producer/consumer stdout and stderr are withheld; only their byte counts and SHA-256
values are recorded. `GH_TOKEN` is scoped to release download, attestation verification,
and the installed resolver's check/stage requests. It has read-only repository
and attestation permissions; apply, repeat-install, and Product invocations run
without that token. The isolated network-failure attempt withholds raw stdout/stderr
and persists only the error code and retryable flag.

`client_curation_browser_submission` is explicitly `NOT_RUN`: the workflow does
not automate a real browser. The required Client curation gate separately checks
the published `/datasets` route, submits the official UI operation through the
same-origin proxy with local CSRF, and verifies the resulting synthetic Product
run through both proxy and direct Catalyst APIs. This is API integration evidence,
not a claim that a human browser completed the page interaction.

`GH_TOKEN` 仅授予仓库与 attestation 只读权限，用于发布物下载、验签以及已安装 resolver 的 check/stage；apply、重复安装与 Product 调用不会继承该 token。

独立安装后 consumer 成功后，artifact 还会提供固定的公开合成验收文件
`evidence/public-fixtures/authored-business-sft.zip` 及其 SHA-256 和字节数；consumer 成功前不会复制到上传目录。

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
5. Verify the installed Web host metadata and live `/` and `/datasets` HTTP routes against the exact Client receipt. After the installed Catalyst producer and independent SFT consumer pass, submit the Client's `curateTrainingData` operation once through the same-origin proxy with the local session CSRF token; require the new fixed-synthetic run to reach `SUCCEEDED` through both proxy and direct Catalyst readback. Then restart `cyrene-catalyst.service`, wait for systemd and HTTP recovery, repeat the public `sudo cyrene workload install catalyst --channel preview --yes` command to prove idempotence, and read back the same exact installed identities.
6. Submit a resolver check that excludes one required Catalyst component; require the expected blocked response and prove that no staging or apply request was sent.

Catalyst 已安装验收模块当前由活动 immutable service bundle 提供；runner 使用固定私有解释器
`/opt/cyrene/python/3.12.14/bin/python3.12` 与活动 bundle 的 `python/` 路径。服务端口默认
`127.0.0.1:8004`；最终命令必须与 rootpins 和已安装 Product listener 配置一致。
`/healthz` 只作为存活检查，producer/consumer 的正式 API 请求才是实际使用证据。

The workflow is deliberately strict: it fails when an expected API or installer
operation is not implemented. Dispatch requires the exact native tag, v2 catalog
binding, and exact Catalyst, Platform-supervised Plugin, Runtime SDK, and Client
release pins. Optional standalone `workloads.plugins` identities are exact-match
plus Runtime SDK pins for the independent stage-only offline retry. Optional Echo
identities are independently pinned and, when supplied, invoke the installed Echo
OCI lifecycle and exact-match evaluation gate; missing, blocked, or failed commands
fail that optional gate. Mid-install fault injection remains `NOT_RUN` until an
official deferred-transaction recovery contract is published, and the hosted VM is
not rebooted by this workflow. Do not substitute the earlier first-product
cohort receipt, a source checkout, a privileged container, or a different native
tag to make a phase pass.

工作流遇到未实现的 API 或安装操作会严格失败。必须等 exact native tag、v2 catalog binding、Catalyst、四个受监管插件、
Runtime SDK 和 Client 的官方发布 pins 都准备好后，才能运行验收。可选 `workloads.plugins` pins 精确包含 exact-match
plugin 与 Runtime SDK，用于独立 stage/retry fallback，不会 apply。可选 Echo pins 精确包含官方 OCI Product、Runtime
SDK 和 exact-match plugin；提供这些 pins 后，工作流在 Catalyst 核心验收后执行已安装 OCI 生命周期、真实 exact-match
评测和受支持的卸载验证，不会从源码启动 Echo 或伪造 Plugin connection reference。中断安装故障注入在官方恢复契约
发布前保持 `NOT_RUN`，本工作流不会重启托管 VM。
不得用旧 first-product cohort receipt、源码 checkout、privileged 容器或其他 native tag 来替代并制造 PASS。
