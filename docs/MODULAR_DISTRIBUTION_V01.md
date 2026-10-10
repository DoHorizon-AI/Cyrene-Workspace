# Modular Distribution v0.1: Ubuntu installation / Ubuntu 安装指南

**Status (Stage71 source correction):** The previously published immutable Native preview W `670dcc640c7716484edacf5c0f77dd7ec93b5fea` (18 assets, bound to the previously verified Platform release P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`) remains unchanged. On Host 2238, the exact Stage70 Catalyst plan passed staging, but `BeginCoreBootstrap` was rejected with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; the original transaction and guest state were preserved, and runtime source activation did not begin. Platform PR #103 is merged at `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`; its focused tests passed 61 total (50 library and 11 binary). The natural official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) is pending publication and strict asset verification at this checkpoint. Do not retry the old W/P pair or describe installation as successful. Client/browser, Plugin invocation, SFT, Echo, reboot, and clean-host acceptance remain **PENDING / NOT_RUN**.

**状态（Stage71 源码修正）：** 此前发布的不可变 Native 预览版 W `670dcc640c7716484edacf5c0f77dd7ec93b5fea`（18 项资产，绑定此前已验证的 Platform release P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`）保持原样。Host 2238 上的 Stage70 Catalyst 精确计划已通过 staging，但 `BeginCoreBootstrap` 返回 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`；原 transaction 与 guest 状态均已保留，runtime source activation 尚未开始。Platform PR #103 已正常合并至 `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`；相关定向测试共 61 项通过（library 50 项、binary 11 项）。截至本文记录时，正式 release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) 尚待发布和严格资产验证。不要重试旧 W/P 发行对，也不要将安装描述为成功。Client/browser、Plugin invoke、SFT、Echo、重启和全新主机验收仍为 **PENDING / NOT_RUN**。

## Start here / 快速开始

Use a stock Ubuntu 24.04 x86_64 host, an administrator account with `sudo`, and an internet connection. Catalyst does not require Docker. The last published Native release is immutable W `670dcc640c7716484edacf5c0f77dd7ec93b5fea`, bound to the previously published Platform P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`; its first-Core admission was blocked on Host 2238. The corrected Platform source is merged, but its official release and a new Workspace-bound Native release have not been verified. Do not run the install commands below against the old pair.

使用标准 Ubuntu 24.04 x86_64 主机、具备 `sudo` 的管理员账户及网络连接。Catalyst 不需要 Docker。最近发布的 Native release 是不可变的 W `670dcc640c7716484edacf5c0f77dd7ec93b5fea`，绑定此前发布的 Platform P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`；它在 Host 2238 的 first-Core admission 被阻止。Platform 修正源码已合并，但正式 release 和绑定新 Workspace SHA 的 Native release 尚未验证。不要对下方命令所示旧发行对执行 workload install。

| Item | Immutable identity or acceptance state |
| --- | --- |
| Previously published Platform release | [`preview-64e4b58eb1dedc5ea328c07c9b07df55d2f38590`](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-64e4b58eb1dedc5ea328c07c9b07df55d2f38590); source P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`; run `38071761850`; release API ID `409115567`; 52 assets, 24 manifests, 6 raw subjects, strict no-skip verification passed. This is the P bound to old Native W670. |
| Platform source correction | [PR #103](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/103), merge `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4`; 61 focused tests passed (50 library, 11 binary); official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) pending; no new release identity or verified assets yet |
| Previously published Native release | [`native-installer-preview-670dcc640c7716484edacf5c0f77dd7ec93b5fea`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-670dcc640c7716484edacf5c0f77dd7ec93b5fea); source W `670dcc640c7716484edacf5c0f77dd7ec93b5fea`; bound Platform source P above; run `38083070781` attempt 1; release API ID `409187818`; 18 assets and strict no-skip verification passed. Immutable but not accepted for install after the Stage70 first-Core block. |
| Ubuntu 24.04 amd64 DEB | [`Download DEB`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/native-installer-preview-670dcc640c7716484edacf5c0f77dd7ec93b5fea/cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb); SHA-256 `4c0fc9f03054f8feb2154c893f5f3fd95ce71650340b099b83571c18bcad9deb`; 155,125,684 bytes |
| Native manifest / source receipt | Manifest SHA-256 `ca1c1d274427fd855d2424e943bc36950c185969e68bf5af9c9b0fc34703c89a`; source receipt SHA-256 `c83a5366c3a55d52386a22fe101ef744305f8459f26295aee12760a53b7fa8d5`; strict verifier bound exact `refs/heads/develop` source W and Platform release P |
| Active Catalog v2 | [`catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad), generation 15; SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d` |
| Host 2238 Stage70 | Catalyst staging **PASS**; first-Core `BeginCoreBootstrap` blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; no runtime source activation; original guest/transaction preserved |
| Remaining acceptance | Catalyst installation/apply, actual Plugin invocation, SFT export/load, Client/browser, Echo, reboot recovery, and independent full-host acceptance **PENDING / NOT_RUN** |

The procedure below records how to verify the last published immutable Native release. It uses public assets and needs no source clone, build environment, GitHub login, private pins file, or private release URL. The old W670/P64 pair is known to stop at first-Core admission; do not install it as a trial candidate. A follow-on Native tag requires a new Workspace source SHA. Do not substitute `latest`; verify the eventual release source ref, source SHA, manifest, source receipt, Catalog, checksums, and detached attestations before installation.

以下流程记录如何验证最近发布的不可变 Native release。它使用公开发行资产，无需 clone 源码、构建环境、GitHub 登录、私有 pins 文件或私有发行 URL。旧 W670/P64 发行对已知会在 first-Core admission 停止，不要将其作为试用候选安装。后续 Native tag 必须绑定新的 Workspace source SHA。不要替换为 `latest`；安装未来发行物前应验证其 source ref、source SHA、manifest、source receipt、Catalog、checksum 和 detached attestation。

## Stage70 Workspace lifecycle and Stage71 Platform correction / Stage70 Workspace 生命周期与 Stage71 Platform 修正

Workspace [PR #140](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/140) merged W source `670dcc640c7716484edacf5c0f77dd7ec93b5fea`; post-merge run [38082850380](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/38082850380) succeeded. Its source fix recognizes signed `python-bundle` Product artifacts when deriving the first source-owner context, keeping the selected Catalyst bundle identity separate from the six-member Core map. The paired published Platform release was P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`. Host 2238 staging passed, but the first `BeginCoreBootstrap` was blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; original transaction and guest state were retained and runtime source activation did not begin. Platform PR #103 is merged at `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` (head `d5126f96059124be36af533e357438d388366099`); its focused suites passed 61 tests (50 library, 11 binary). Natural official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) is pending publication and strict verification; no new P release identity is available yet.

The initial activity source is bound to the exact signed Product owner and artifact digest separately from the six-member Core component map. Workspace carries generic `initial_source_artifact_ref` in the first Core bootstrap context. The Stage70 published Platform P64 rejected `BeginCoreBootstrap` against the empty generation-0 catalog, so Host 2238 did not reach the later Product startup phase; the new Platform source in PR #103 adds the guarded generation-0-to-generation-1 contract.

With the corrected Platform release, the first-install order starts the verified Product under the narrow initial-source hold so its authenticated `TasksReconciled` event establishes real source freshness. The Stage70 P64 release did not reach this phase. After that first-source hold ends, Workspace uses the existing package-only Plugin installation and binding flow, projects selected refs into the separate root-owned `root:cyrene` mode `0640` protected `catalyst-plugin-refs.env`, and runs the normal Core-runtime activation/restart so Catalyst reads actual refs. Raw refs are not stored in the journal. Same-plan recovery reconciles exact Begin/End receipts and preserves the hold token after an unsuccessful startup.

At service boot, the packaged Workspace helper calls `ComponentUpdater.recover_workload_runtime_connections("catalyst")` before Catalyst starts. When a successful managed Catalyst transaction owns bindings, it uses the official Package Runtime `recover_binding` and `runtime_status` path to re-establish each binding, reads the current connection refs, and atomically projects the protected environment file. Catalyst is ordered after Package Runtime with start ordering that does not propagate Package Runtime stops to Catalyst. The hook skips only when there is no managed Catalyst owner or an exact installer-controlled restart intent proves the updater currently owns the relevant lock; unrelated lock conflicts and recovery failures fail closed. This protects dynamic runtime endpoints across host restarts without manually writing refs or installing helpers.

## Platform first-source contract / Platform 首次 source 契约

Platform [PR #103](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/103) merged as `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` (head `d5126f96059124be36af533e357438d388366099`). Its focused tests passed 61 tests: 50 library and 11 binary tests. The source change is merged; natural official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) has not yet produced a release verified by this guide. No release ID, asset digest, or attestation result is claimed.

The generic initial-source path has three guards:

1. The exact successful `BeginCoreBootstrap` parent may reserve `initial_source_artifact_ref = {source_id, component_id, artifact_digest}` from a verified signed Product identity, outside the six-member Core component digest map.
2. With that matching hold proof, the initial activity-source catalog may transition only from generation 0 with no registered sources or prior activity to generation 1 containing exactly the reserved source with zero binding scopes. Wrong owners, extra sources, bindings, or repeat initial writes are rejected.
3. `BeginInitialSourceActivation` must match the successful parent/source and a child `CORE_RUNTIME` plan carrying the same Product digest. Only a never-active source is eligible; ordinary readiness and active/stale-source checks remain fail-closed. `EndMaintenance` requires fresh authenticated source state and quiescent activity while the hold is active; missing or stale freshness does not unlock it.

The Platform path does not fabricate activity. The Product's authenticated `TasksReconciled` event establishes persisted freshness; periodic heartbeats continue through the normal SDK lifecycle. Catalog generation 15 and existing Product/Plugin pins remain unchanged. A new Native release needs a new Workspace SHA because the immutable tag is Workspace-source-specific; this documentation source update is intended to provide that identity after the Platform release is verified.

Platform [PR #103](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/103) 已于 `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` 合并（PR head `d5126f96059124be36af533e357438d388366099`）。定向测试共 61 项通过：library 50 项、binary 11 项。源码已合并；正式 release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) 尚未产生经本文核验的 release，因此这里不声明 release ID、资产摘要或 attestation 结果。

通用首次 source 路径有三项守卫：

1. 精确成功的 `BeginCoreBootstrap` parent 可从已验证的签名 Product identity 保留 `initial_source_artifact_ref = {source_id, component_id, artifact_digest}`，并与六成员 Core component digest map 分开保存。
2. 持有匹配的 maintenance proof 时，初始 activity-source catalog 只能从 generation 0（无已注册 source、无既有 activity）转到 generation 1，且只含与预留 identity 完全一致、binding scope 为空的单个 source。错误 owner、多余 source、已有 binding 或重复初始写入都会被拒绝。
3. `BeginInitialSourceActivation` 必须匹配成功 parent/source，并要求 child `CORE_RUNTIME` plan 携带相同 Product digest。只有从未活动的 source 符合条件；普通 readiness 与已有/过期 source 检查保持 fail closed。`EndMaintenance` 在 hold 仍有效时要求真实认证 source 状态新鲜且 activity 静止；freshness 缺失或过期时不会解锁。

Platform 路径不会伪造 activity。Product 的认证 `TasksReconciled` event 建立持久化 freshness；周期 heartbeat 继续由正常 SDK 生命周期发送。Catalog generation 15 和现有 Product/Plugin pins 保持不变。由于不可变 Native tag 由 Workspace source SHA 确定，新 Native release 必须使用新的 Workspace SHA；本次文档 source 更新计划在 Platform release 通过验证后提供该身份。

**Stage70 acceptance status:** The immutable old Platform/Native releases and exact pins remain strictly verified; Workspace post-merge CI **PASS**. Host 2238 Catalyst staging **PASS**, but first-Core `BeginCoreBootstrap` was blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; runtime source activation did not begin and original state was preserved. The new Platform P release and all later Catalyst/runtime, Plugin invocation, SFT, Client/browser, Echo, reboot, and independent-host gates remain **PENDING / NOT_RUN**.

命令语法沿用已发布 preview CLI，可用于当前 Stage70 精确不可变 release 的后续验收；本节仅记录语法，不代表这些命令已经在 Host 2238 上完成：

```bash
sudo cyrene workload install catalyst --channel preview --yes
sudo cyrene workload install echo --channel preview --yes
printf '%s\n' \
  '{"protocolVersion":"cyrene.workload-plan.v1","operation":"status","workloadId":"catalyst"}' \
  | sudo cyrene workload --json | jq .
```

Workspace [PR #140](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/140) merged W source `670dcc640c7716484edacf5c0f77dd7ec93b5fea`; post-merge run [38082850380](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/38082850380) succeeded. Its source fix recognizes signed `python-bundle` Product artifacts when deriving the first source-owner context, keeping the selected Catalyst bundle identity separate from the six-member Core map. The paired published Platform release was P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`. Host 2238 staging passed, but the first `BeginCoreBootstrap` was blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; original transaction and guest state were retained and runtime source activation did not begin. Platform PR #103 is merged at `1e1a8d37b73d057bf63d2f57898f565e78cfdbd4` (head `d5126f96059124be36af533e357438d388366099`); its focused suites passed 61 tests (50 library, 11 binary). Natural official release run [38089871402](https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/38089871402) is pending publication and strict verification; no new P release identity is available yet.

首次 activity source 与精确签名 Product owner 和 artifact digest 绑定，并与六成员 Core component map 分开。Workspace 将通用 `initial_source_artifact_ref` 放入 first Core bootstrap context 和精确 parent transaction。Stage70 的已发布 Platform P64 在空 generation-0 catalog 上拒绝了 `BeginCoreBootstrap`，所以 Host 2238 未进入后续 Product 启动阶段；PR #103 的新 Platform source 才增加受守卫的 generation-0-to-generation-1 契约。

使用修正后的 Platform release 时，首次安装会在范围狭窄的 initial-source hold 内启动已验签 Product，由其认证 `TasksReconciled` 建立真实 source freshness。Stage70 的 P64 release 未能进入此阶段。该 hold 结束后，Workspace 使用既有 package-only Plugin 安装和 binding 流程，将选中 connection ref 投影到 root-owned `root:cyrene`、权限 `0640` 的受保护 `catalyst-plugin-refs.env`，再执行普通 Core-runtime activation/restart，使 Catalyst 读取实际 refs。原始 ref 不写入 journal。同 plan 恢复对账精确 Begin/End receipt，并在启动失败后保留 hold token。

系统启动时，打包在 Workspace 中的 helper 会在 Catalyst 启动前调用 `ComponentUpdater.recover_workload_runtime_connections("catalyst")`。存在受管 Catalyst transaction 和 binding 时，它通过官方 Package Runtime recovery/status 路径重新建立 binding、读取当前 connection ref，并原子投影受保护环境文件。Catalyst 的启动顺序在 Package Runtime 之后，但 Package Runtime 停止不会传递停止 Catalyst。只有在没有受管 Catalyst owner，或精确 installer-controlled restart intent 证明 updater 正持有相关锁时，hook 才会 skip；其他锁冲突和恢复错误均 fail closed。该机制用于主机重启后恢复动态 runtime endpoint，不需要手工写 refs 或安装 helper。

**Stage70 验收状态：** 旧 Platform/Native 不可变发行和精确 pins 仍通过严格验证；Workspace 合并后 CI **PASS**。Host 2238 Catalyst staging **PASS**，但 first-Core `BeginCoreBootstrap` 因 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED` 被阻止；runtime source activation 未开始，原状态已保留。新的 Platform P release 及后续 Catalyst/runtime、Plugin invoke、SFT、Client/browser、Echo、重启和独立主机门禁仍为 **PENDING / NOT_RUN**。

Only after Catalyst and Client activation should the user open `http://127.0.0.1:8100/` on the Ubuntu host. For remote use, keep the service bound to loopback, replace `user@ubuntu-host` with the actual account and host, and use an approved SSH local forward:

```bash
ssh -L 8100:127.0.0.1:8100 user@ubuntu-host
```

只有 Catalyst 和 Client 激活后，才在 Ubuntu 主机打开 `http://127.0.0.1:8100/`。服务应保持 loopback 绑定；远程访问时，将 `user@ubuntu-host` 替换为实际账户和主机，通过获准的 SSH 本地转发连接。

### Host prerequisites / 主机前置条件

Use Ubuntu 24.04 amd64. The following host preparation installs only the
required transfer and JSON tools, then installs or upgrades `gh` from GitHub's
official Debian/Ubuntu APT keyring and `signed-by` source if it is absent or
below the minimum. An existing `gh` that already meets the minimum is left
unchanged. The APT command targets `gh`; this block does not run a system
upgrade. The minimum is GitHub CLI **2.102.0**; the version gate and help check
below fail before any release download if it is still too old or lacks
attestation support.

The trusted updater also requires installation prefixes to be root-owned and
not group/world-writable. The clean Ubuntu 24.04.5 acceptance guest had
`/usr/share` and `/opt` at root-owned mode `0755`. A hosted or custom image with
either prefix writable by group or other users fails closed; use a stock
Ubuntu image instead of changing permissions to bypass the guard.

使用 Ubuntu 24.04 amd64。以下主机准备命令安装必需的下载和 JSON 工具；若 `gh` 缺失或低于最低版本，则通过
GitHub 官方 Debian/Ubuntu APT keyring 与 `signed-by` 源安装或升级。已有版本满足最低要求时不作更改。APT 命令只针对
`gh`，此处不执行系统升级。最低版本为 GitHub CLI **2.102.0**；若最终版本仍过旧或不支持 attestation，版本门禁和
help 检查会在下载发行物前失败。

可信 updater 还要求安装前缀由 root 所有且不可由组或其他用户写入。clean Ubuntu 24.04.5 验收主机的
`/usr/share` 与 `/opt` 均为 root 所有、权限 `0755`。若托管或自定义镜像的任一路径允许组或其他用户写入，guard 会拒绝继续；应使用标准 Ubuntu 镜像，不要修改权限绕过检查。

```bash
set -euo pipefail
sudo apt-get update
sudo apt-get install --no-install-recommends -y ca-certificates curl jq

GH_VERSION=''
if command -v gh >/dev/null 2>&1; then
  GH_VERSION="$(gh --version | awk 'NR == 1 {print $3}')"
fi

if [[ -z "$GH_VERSION" ]] || ! dpkg --compare-versions "$GH_VERSION" ge 2.102.0; then
  sudo install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update
  sudo apt-get install --no-install-recommends -y gh
fi

gh --version
GH_VERSION="$(gh --version | awk 'NR == 1 {print $3}')"
dpkg --compare-versions "$GH_VERSION" ge 2.102.0
jq --version
gh attestation verify --help >/dev/null
```

This flow needs no `gh auth login`: it downloads public release assets over
HTTPS and verifies each subject against its locally downloaded detached
attestation bundle. Keep the official signer-workflow, source-ref, source-SHA,
manifest, receipt, and Catalog checks below intact.

此流程无需 `gh auth login`：它通过 HTTPS 下载公开发行物，并使用本地 detached attestation bundle 验证每个 subject。
以下官方 signer-workflow、source-ref、source-SHA、manifest、receipt 和 Catalog 校验必须完整保留。

## 1. Current Native preview and verified downloads / 当前 Native 预览版与验签下载

The current immutable Native release is Workspace W `670dcc640c7716484edacf5c0f77dd7ec93b5fea` ([release page](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-670dcc640c7716484edacf5c0f77dd7ec93b5fea)), bound to the strictly verified Platform P release [`preview-64e4b58eb1dedc5ea328c07c9b07df55d2f38590`](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-64e4b58eb1dedc5ea328c07c9b07df55d2f38590). Native run [38083070781](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/38083070781) attempt 1 completed all four jobs successfully; immutable release API ID `409187818` contains 18 actual assets. Strict no-skip verification bound 21 source tuples and 8 exact source-verifier files to W/P. The Ubuntu 24.04 DEB is 155,125,684 bytes, SHA-256 `4c0fc9f03054f8feb2154c893f5f3fd95ce71650340b099b83571c18bcad9deb`; manifest SHA-256 is `ca1c1d274427fd855d2424e943bc36950c185969e68bf5af9c9b0fc34703c89a`, and source receipt SHA-256 is `c83a5366c3a55d52386a22fe101ef744305f8459f26295aee12760a53b7fa8d5`. The bound Platform release has 52 verified assets, 24 manifests, and 6 raw manifest subjects. Release publication and pins verification do not prove Host 2238 product acceptance; Stage70 first-Core admission was blocked and all later gate results remain pending.

当前不可变 Native release 基于 Workspace W `670dcc640c7716484edacf5c0f77dd7ec93b5fea` （[发行页](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-670dcc640c7716484edacf5c0f77dd7ec93b5fea)），绑定已严格验证的 Platform P release [`preview-64e4b58eb1dedc5ea328c07c9b07df55d2f38590`](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-64e4b58eb1dedc5ea328c07c9b07df55d2f38590)。Native run [38083070781](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/38083070781) attempt 1 的四个 job 全部成功；不可变 release API ID `409187818` 含 18 个实际资产。严格无跳过验证将 21 个 source tuple 和 8 个精确源码 verifier 文件绑定到 W/P。Ubuntu 24.04 DEB 大小为 155,125,684 字节，SHA-256 为 `4c0fc9f03054f8feb2154c893f5f3fd95ce71650340b099b83571c18bcad9deb`；manifest SHA-256 为 `ca1c1d274427fd855d2424e943bc36950c185969e68bf5af9c9b0fc34703c89a`，source receipt SHA-256 为 `c83a5366c3a55d52386a22fe101ef744305f8459f26295aee12760a53b7fa8d5`。绑定的 Platform release 有 52 个已验证资产、24 个 manifest 和 6 个原始 manifest subject。发行发布与 pins 验证不能代替 Host 2238 产品验收；Stage70 first-Core admission 已被阻止，其余 gate 结果仍待完成。

Use an Ubuntu 24.04 amd64 host. The values below pin the published native
release and the active-v2 Catalog recorded in its verified source receipt. Do
not substitute a `latest` release or change an identity without verifying its
exact immutable release and detached attestations.

使用 Ubuntu 24.04 amd64 主机。下列值固定了已发布的 native release，以及其已验签 source receipt 记录的 active-v2
Catalog。不得用 `latest` 代替，也不得在未验证确切不可变发行物和 detached attestation 前修改身份：

```bash
WORKSPACE_REPOSITORY='DoHorizon-AI/Cyrene-Workspace'
NATIVE_RELEASE_ID='native-installer-preview-670dcc640c7716484edacf5c0f77dd7ec93b5fea'
NATIVE_SOURCE_REF='refs/heads/develop'
NATIVE_SOURCE_SHA='670dcc640c7716484edacf5c0f77dd7ec93b5fea'
NATIVE_DEB_ASSET='cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb'
NATIVE_MANIFEST_ASSET='native-installer-release-v2.json'
NATIVE_SOURCE_RECEIPT_ASSET='native-installer-source-receipt-v2.json'
CATALOG_RELEASE_ID='catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad'
CATALOG_SOURCE_REF='refs/heads/develop'
CATALOG_SOURCE_SHA='18455255cdf8b87dd8cd48ceb25e5ea411a622ad'
CATALOG_SHA256_EXPECTED='9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d'
CATALOG_ATTESTATION_SHA256_EXPECTED='37762cbdabe99c5e62fa9494531ff7e16962b241d2cff47bf920530a33585e85'
RELEASE_DIR="$(mktemp -d)"
```

Download the exact public GitHub release assets over HTTPS; no repository
clone, source build, or GitHub login is needed. First verify the detached attestation for
`SHA256SUMS`, then check the downloaded payload checksums. Verify the DEB and v2
release manifest against the exact repository, publisher workflow, source ref,
and source SHA.
Verify the v2 source receipt's active-v2 catalog release ID and source identity,
then verify its exact catalog bytes, generation, and detached attestation. Stop
if any identity differs or if the release lacks the v2 manifest, source receipt,
DEB/catalog attestation bundles, or expected generation-15 catalog binding.

通过 HTTPS 下载确切的公开 GitHub release assets，无需 clone 仓库、构建源码或登录 GitHub。先验证
`SHA256SUMS` 的 detached attestation，
再校验下载的 payload checksum。然后按确切仓库、发布 workflow、source ref 和 source SHA 验证 DEB 与 v2
release manifest。再校验 v2 source receipt 中 active-v2 Catalog 的 release ID/source identity，并验证其确切
Catalog bytes、generation 与 detached attestation。任一身份不符，或缺少 v2 manifest、source receipt、DEB/Catalog
attestation bundle 或预期的 generation-15 Catalog binding，都应停止安装。

The signed checksum file covers both platform DEBs. This guide downloads only
the Ubuntu 24.04 target plus the exact manifest, receipt, and Catalog assets it
uses; `--ignore-missing` therefore skips undownloaded release assets, while
checking every downloaded payload. Required files are also checked for
presence below.

签名 checksum 文件覆盖两个平台的 DEB。本指南只下载 Ubuntu 24.04 目标及实际使用的 manifest、receipt 和 Catalog；
因此 `--ignore-missing` 只跳过未下载的发行资产，并校验所有已下载 payload。下方还会检查必需文件是否存在。

```bash
set -euo pipefail
mkdir -p "$RELEASE_DIR"
RELEASE_URL="https://github.com/$WORKSPACE_REPOSITORY/releases/download/$NATIVE_RELEASE_ID"
RELEASE_ASSETS=(
  SHA256SUMS
  SHA256SUMS.attestation.jsonl
  "$NATIVE_DEB_ASSET"
  "$NATIVE_DEB_ASSET.attestation.jsonl"
  "$NATIVE_MANIFEST_ASSET"
  "$NATIVE_MANIFEST_ASSET.attestation.jsonl"
  "$NATIVE_SOURCE_RECEIPT_ASSET"
  "$NATIVE_SOURCE_RECEIPT_ASSET.attestation.jsonl"
  component-catalog-v2.json
  component-catalog-v2.json.attestation.jsonl
)
for asset in "${RELEASE_ASSETS[@]}"; do
  curl --fail --location --silent --show-error \
    --output "$RELEASE_DIR/$asset" "$RELEASE_URL/$asset"
done

cd "$RELEASE_DIR"
gh attestation verify SHA256SUMS \
  --bundle SHA256SUMS.attestation.jsonl \
  --repo "$WORKSPACE_REPOSITORY" \
  --signer-workflow "$WORKSPACE_REPOSITORY/.github/workflows/native-installer-release.yml" \
  --source-ref "$NATIVE_SOURCE_REF" \
  --source-digest "$NATIVE_SOURCE_SHA" \
  --predicate-type https://slsa.dev/provenance/v1

sha256sum --check --strict --ignore-missing SHA256SUMS
printf '%s  %s\n' \
  '4c0fc9f03054f8feb2154c893f5f3fd95ce71650340b099b83571c18bcad9deb' "$NATIVE_DEB_ASSET" \
  'ca1c1d274427fd855d2424e943bc36950c185969e68bf5af9c9b0fc34703c89a' "$NATIVE_MANIFEST_ASSET" \
  'c83a5366c3a55d52386a22fe101ef744305f8459f26295aee12760a53b7fa8d5' "$NATIVE_SOURCE_RECEIPT_ASSET" \
  | sha256sum --check --strict
test -s "$NATIVE_DEB_ASSET"
test -s "$NATIVE_DEB_ASSET.attestation.jsonl"
test -s "$NATIVE_MANIFEST_ASSET"
test -s "$NATIVE_MANIFEST_ASSET.attestation.jsonl"
test -s "$NATIVE_SOURCE_RECEIPT_ASSET"
test -s "$NATIVE_SOURCE_RECEIPT_ASSET.attestation.jsonl"
test -s component-catalog-v2.json
test -s component-catalog-v2.json.attestation.jsonl

CATALOG_SHA256="$(sha256sum component-catalog-v2.json | cut -d ' ' -f 1)"
CATALOG_ATTESTATION_SHA256="$(sha256sum component-catalog-v2.json.attestation.jsonl | cut -d ' ' -f 1)"
test "$CATALOG_SHA256" = "$CATALOG_SHA256_EXPECTED"
test "$CATALOG_ATTESTATION_SHA256" = "$CATALOG_ATTESTATION_SHA256_EXPECTED"
jq -e --arg tag "$CATALOG_RELEASE_ID" \
  --arg ref "$CATALOG_SOURCE_REF" \
  --arg source "$CATALOG_SOURCE_SHA" \
  --arg catalog "$CATALOG_SHA256" \
  --arg bundle "$CATALOG_ATTESTATION_SHA256" \
  '.releaseInputs.workspaceCatalogs.activeV2
   | .releaseId == $tag and .generation == 15 and .source.ref == $ref
     and .source.commit == $source and .assetName == "component-catalog-v2.json"
     and .sha256 == $catalog and .attestationBundleSha256 == $bundle' \
  "$NATIVE_SOURCE_RECEIPT_ASSET" >/dev/null
jq -e '.schemaVersion == 2 and .generation == 15' \
  component-catalog-v2.json >/dev/null

gh attestation verify "$NATIVE_DEB_ASSET" \
  --bundle "$NATIVE_DEB_ASSET.attestation.jsonl" \
  --repo "$WORKSPACE_REPOSITORY" \
  --signer-workflow "$WORKSPACE_REPOSITORY/.github/workflows/native-installer-release.yml" \
  --source-ref "$NATIVE_SOURCE_REF" \
  --source-digest "$NATIVE_SOURCE_SHA" \
  --predicate-type https://slsa.dev/provenance/v1

gh attestation verify "$NATIVE_MANIFEST_ASSET" \
  --bundle "$NATIVE_MANIFEST_ASSET.attestation.jsonl" \
  --repo "$WORKSPACE_REPOSITORY" \
  --signer-workflow "$WORKSPACE_REPOSITORY/.github/workflows/native-installer-release.yml" \
  --source-ref "$NATIVE_SOURCE_REF" \
  --source-digest "$NATIVE_SOURCE_SHA" \
  --predicate-type https://slsa.dev/provenance/v1

gh attestation verify "$NATIVE_SOURCE_RECEIPT_ASSET" \
  --bundle "$NATIVE_SOURCE_RECEIPT_ASSET.attestation.jsonl" \
  --repo "$WORKSPACE_REPOSITORY" \
  --signer-workflow "$WORKSPACE_REPOSITORY/.github/workflows/native-installer-release.yml" \
  --source-ref "$NATIVE_SOURCE_REF" \
  --source-digest "$NATIVE_SOURCE_SHA" \
  --predicate-type https://slsa.dev/provenance/v1

gh attestation verify component-catalog-v2.json \
  --bundle component-catalog-v2.json.attestation.jsonl \
  --repo "$WORKSPACE_REPOSITORY" \
  --signer-workflow "$WORKSPACE_REPOSITORY/.github/workflows/component-catalog-release.yml" \
  --source-ref "$CATALOG_SOURCE_REF" \
  --source-digest "$CATALOG_SOURCE_SHA" \
  --predicate-type https://slsa.dev/provenance/v1

sudo apt install "./$NATIVE_DEB_ASSET"
```

If the same Debian version is already installed, use `sudo apt install --yes --reinstall <DEB-path>`. The package driver verifies the installed member SHA before reporting success.

若系统已安装相同 Debian 版本，使用 `sudo apt install --yes --reinstall <DEB-path>`。package driver 会在报告成功前校验已安装成员的 SHA。

Use only the DEB for Ubuntu 24.04 amd64. Keep the release tag, source SHA,
manifest, checksum output, and attestation verification result with the test
record. The DEB-installed helper and its trusted Catalog—not a source checkout
or manually copied catalog—resolve component release indexes, manifests,
artifacts, and attestations before staging.

只安装 Ubuntu 24.04 amd64 对应的 DEB。测试记录保留 release tag、source SHA、manifest、checksum
输出和 attestation 验证结果。组件 release index、manifest、artifact 和 attestation 由 DEB 安装的
可信 helper 与 Catalog 解析；不得用源码 checkout 或手工复制的 Catalog 代替。

## 2. Install Catalyst / 安装 Catalyst

Read-only status uses the fixed `cyrene.workload-plan.v1` JSON protocol. It reads
local receipts and does not fetch or change releases:

只读状态查询使用固定的 `cyrene.workload-plan.v1` JSON 协议；它读取本机 receipt，不下载或修改发行物：

```bash
printf '%s\n' \
  '{"protocolVersion":"cyrene.workload-plan.v1","operation":"status","workloadId":"catalyst"}' \
  | sudo cyrene workload --json | jq .
```

**Stage70 host-acceptance gate:** The old Native release and pins are verified as immutable artifacts, but Host 2238 staging passed and first-Core `BeginCoreBootstrap` was blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`. Do not run the install/apply command below against old W670/P64. Remaining gates are **PENDING / NOT_RUN**.

**Stage70 主机验收门禁：** 旧 Native release 和 pins 已作为不可变发行物完成核验；Host 2238 staging 通过，但 first-Core `BeginCoreBootstrap` 因 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED` 被阻止。不要对旧 W670/P64 执行下方 install/apply 命令。后续门禁仍为 **PENDING / NOT_RUN**。

The guided command checks the Catalog, displays the resolved component identities,
stages that exact plan, and applies it only with its matching plan ID and digest.
`--yes` confirms the plan produced by that invocation; it does not skip check,
stage, digest validation, or the trusted updater. Since the refs in this guide
are previews, select that channel explicitly. The default Catalyst selection
contains the required Catalyst service, Client Control, Client Web, and Runtime
Maintenance SDK, plus four recommended data plugins:

向导命令检查 Catalog、显示解析出的组件身份、暂存精确计划，并且只按相同 plan ID 与 digest 执行 apply。
`--yes` 只确认本次命令生成的计划，不跳过 check、stage、digest 校验或可信 updater。本文引用的是 preview 发行物，
因此要显式选择该通道。Catalyst 默认选择包含
必需的 Catalyst 服务、Client Control、Client Web、Runtime Maintenance SDK，以及四个推荐数据插件：

- `cyrene-tools-dataset-preparation`
- `cyrene-tools-document-parsing`
- `cyrene-tools-dataset-generation`
- `cyrene-tools-knowledge-preparation`

On a fresh host, the normal checked Catalyst workload plan performs the
official first-Core bootstrap during `apply`: it initializes the missing
activity-source catalog and starts required Core components from the exact
verified plan. The signed Plugin release provisions a generic preparer from a
selected Plugin wheel in that same plan, after checking the staged release
identity, digest, and attestation. The Stage70 source fix also recognizes signed `python-bundle` Product artifacts
when binding the first activity source owner, so the selected Catalyst bundle
carries its exact component and artifact digest. The preparer identity stays
bound to the parent workload plan while the C10 child bootstrap identity
remains distinct;
an interrupted apply can continue through the saved-resolution path for that
exact plan. No manual helper installation, separate Core recovery command,
source checkout, or `PYTHONPATH` is required. Review the check and staged
component identities; if bootstrap reports a blocker, keep the receipt and
error details and stop rather than editing runtime state by hand. This source
path is merged, while host acceptance is still in progress.

全新主机上的 Catalyst 正常 checked workload plan 会在 `apply` 期间执行官方 first-Core bootstrap：基于精确核验的 plan 初始化缺失的 activity-source catalog，并启动必需的 Core 组件。Workspace 会在校验 staged release identity、digest 和 attestation 后，从同一 plan 所选 Plugin 的 wheel 配置通用 preparer。Stage70 源码修复还将已签名的 `python-bundle` Product 纳入首次 activity source owner 绑定，使所选 Catalyst bundle 保留精确的 component 与 artifact digest。preparer 身份绑定 parent workload plan，C10 child bootstrap 身份保持独立；apply 中断后可通过 saved-resolution path 继续同一个精确 plan。无需手工安装 helper、运行单独的 Core 恢复命令、checkout 源码或设置 `PYTHONPATH`。检查 check 和 staged component 身份；若 bootstrap 报告 blocker，应保留 receipt 与错误详情并停止，不要手工修改 runtime state。该源码路径已合并；主机验收仍在进行。

The Host 2238 Stage70 attempt used only Workspace W `670dcc640c7716484edacf5c0f77dd7ec93b5fea`, Platform P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590`, and their bound immutable Native release. Actual status: Catalyst staging **PASS**; first-Core `BeginCoreBootstrap` blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; original guest/transaction preserved; runtime source activation and all later runtime/product gates **PENDING / NOT_RUN**. Platform PR #103 is merged, but its official release remains pending.

Host 2238 的 Stage70 尝试仅使用 Workspace W `670dcc640c7716484edacf5c0f77dd7ec93b5fea`、Platform P `64e4b58eb1dedc5ea328c07c9b07df55d2f38590` 及其绑定的不可变 Native release。实际状态：Catalyst staging **PASS**；first-Core `BeginCoreBootstrap` 因 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED` 被阻止；原 guest/transaction 已保留；runtime source activation 及所有后续 runtime/product 门禁 **PENDING / NOT_RUN**。Platform PR #103 已合并，但正式 release 仍待完成。

## 3. Install one standalone plugin / 单独安装一个插件

Catalog generation 15 defines five signed plugin-package identities:
the four Catalyst data plugins above and `cyrene-evaluation-exact-match`. To
install one plugin without installing Catalyst, Echo, or Client, use workload
`plugins` and explicitly include one component. The Catalog adds the required
Runtime Maintenance SDK; it does not add a Product or Web Client.

Catalog generation 15 定义五个 signed plugin-package identity：上述四个 Catalyst 数据插件和
`cyrene-evaluation-exact-match`。若只安装一个插件且不安装 Catalyst、Echo 或 Client，请使用
`plugins` workload 并显式纳入一个组件。Catalog 会添加必需的 Runtime Maintenance SDK，但不会添加 Product
或 Web Client。

Example `check` for Document Parsing; its package is a preview, so set
`channel` to `preview`. If ready, use its exact `planId` and `planDigest` in the
stage and apply requests shown above, changing `workloadId` to `plugins` and
preserving the same selection and channel:

以下以 Document Parsing 为例执行 `check`，其发行物属于 preview，因此 channel 设为 `preview`；若状态为 `ready`，
在上文 stage/apply 请求中将 `workloadId` 改为 `plugins`，并使用 check 返回的精确 `planId`、`planDigest` 与 channel：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"check","workloadId":"plugins","channel":"preview","targetId":"linux-ubuntu-24.04-x86_64","action":"install","selections":{"includeComponentIds":["cyrene-tools-document-parsing"],"excludeComponentIds":[],"choices":{}}}
JSON
```

The `plugins` workload requires at least one explicit optional selection. An
empty selection is blocked; it does not install every plugin. Each selected
package must resolve through the signed Catalog and exact publisher release
index/manifest/attestation chain.

`plugins` workload 至少要求一个显式可选选择项。空选择会被 blocker 拒绝，不会安装全部插件。每个选择的包都必须
通过签名 Catalog 和确切 publisher release index/manifest/attestation 链解析。

## 4. Add Echo / 增装 Echo

Echo is a separate optional workload; its Linux OCI preview release is listed
above. Catalyst uses a native bundle and does **not** require Docker. Selecting
Echo requires Docker Engine installed using Docker's official Ubuntu instructions; use its supported APT repository procedure, review any existing conflicting packages before changing them, and avoid the convenience script for production hosts. Keep the daemon active and the standard Unix socket available to the privileged workload operation. After following the [official installation guide](https://docs.docker.com/engine/install/ubuntu/), verify the service and a test container before selecting Echo:

Echo 是单独的可选 workload，其 Linux OCI 预览发行物见上表。Catalyst 使用原生 bundle，**不需要 Docker**。
选择 Echo 需要安装官方 [Ubuntu Docker Engine](https://docs.docker.com/engine/install/ubuntu/)，并确保 daemon 正在
运行，且特权 workload 操作可访问标准 Unix socket。选择 Echo 前检查主机：

```bash
sudo systemctl is-active docker
sudo docker info
sudo docker run --rm hello-world
test -S /var/run/docker.sock
```

Do not add users to the `docker` group or loosen socket permissions as a
shortcut; [Docker documents](https://docs.docker.com/engine/install/linux-postinstall/#manage-docker-as-a-non-root-user)
that group as granting root-level privileges. Keep the host's existing
administrator-approved access controls.

不要把用户加入 `docker` 组或放宽 socket 权限作为捷径；Docker 文档说明该组具有 root 级权限。保留主机现有且经
管理员批准的访问控制。

The published generation-15 Catalog pins Echo's exact OCI artifact. Select the
`preview` channel to add Echo and its default recommended exact-match evaluator
without replacing or uninstalling Catalyst:

已发布的 generation-15 Catalog 固定了 Echo 的确切 OCI artifact。显式选择 `preview` 通道即可增装 Echo 及默认推荐的
exact-match evaluator；该命令不会替换或卸载 Catalyst：

```bash
sudo cyrene workload install echo --channel preview --yes
```

After evaluation, remove only Echo with the guided workload plan. Catalyst and the user's datasets, artifacts, review history, and backups remain in place:

评测完成后，使用 guided workload plan 只卸载 Echo。Catalyst 及用户 Dataset、Artifact、审核历史和备份会保留：

```bash
sudo cyrene workload uninstall cyrene-echo --workload echo --channel preview --yes
```

To omit the evaluator, use the same check/stage/apply protocol with workload
`echo`, `channel: "preview"`, and
`excludeComponentIds: ["cyrene-evaluation-exact-match"]`; repeat the same
channel for stage and apply. The official release and signed Catalog path are available; source-matched host evidence for Echo installation, evaluation, and lifecycle acceptance is pending.

若不安装 evaluator，使用相同的 check/stage/apply 协议并设置 workload `echo`、`channel: "preview"` 与
`excludeComponentIds: ["cyrene-evaluation-exact-match"]`；stage 和 apply 重复相同 channel。官方发行物和签名 Catalog
路径已就绪；匹配源码的 Echo 安装、评测与 lifecycle 主机证据仍待完成。

## 5. Data, secrets, and acceptance / 数据、凭据与验收

Workload installation is additive. It preserves user datasets, artifacts,
review history, backups, and existing SSH forwarding configuration by default.
The updater uses its existing receipts and transaction journal; installation
does not create a second database.
The local Control setup generates and stores its protected token and Catalyst
environment/drop-in on first installation, then reuses that protected token on
repeat installation. Do not create, paste, rotate, or place Control tokens in
command lines or this document. Keep Control bound to loopback; access the Web
host through the same-origin Nginx route and an existing approved SSH forward.

Workload 安装采用增量方式，默认保留用户 Dataset、Artifact、审核历史、备份和既有 SSH 转发配置。本机 Control
使用 updater 现有 receipts 和 transaction journal，不会新建第二套数据库。首次安装时生成并保存受保护的 token 与
Catalyst environment/drop-in，重复安装复用该受保护 token。不要自行创建、粘贴、轮换 Control token，也不要将其写入
命令行或本文。Control 保持 loopback 绑定；Web 通过同源 Nginx 路由和已有且获准的 SSH 转发访问。

### Rollback and retained data / 回滚与数据保留

The preview CLI documents workload install and uninstall; it does not expose a user-selected release downgrade command. The Platform package runtime can roll back a managed maintenance transaction, but that mechanism is not a promise that an operator can select any prior workload release. On failure, keep the plan, receipts, and transaction journal, stop on the reported blocker, and use the supported maintenance path; do not edit Broker/runtime state or manually replace package files. Workload uninstall removes the selected workload, not its user data. The default lifecycle preserves datasets, artifacts, review history, and backups, but retention is not a substitute for an independently maintained backup.

此预览 CLI 文档提供 workload install 与 uninstall，没有面向用户的指定发行版本降级命令。Platform package runtime 可回滚受管理的 maintenance transaction，但这不表示操作者可以任意选择过去的 workload 版本。发生失败时保留 plan、receipt 与 transaction journal，按报告的 blocker 停止并使用受支持的 maintenance 流程；不要编辑 Broker/runtime 状态，也不要手动替换包文件。卸载 workload 会移除所选 workload，不会删除用户数据。默认生命周期保留 Dataset、Artifact、审核历史和备份，但这不能替代单独维护的备份。

### Acceptance status / 验收状态

The previously published P64 and W670 releases remain immutable and strictly verified. On Host 2238, Catalyst staging **PASS** but first-Core Begin was blocked with `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED`; original transaction and guest state were preserved, and runtime source activation did not begin. Platform PR #103 is merged, but its official release is still pending. Actual Catalyst runtime, Plugin invocation, boot-time binding recovery/restart, Client/browser, SFT, Echo, and full independent-host acceptance remain **PENDING / NOT_RUN**. Do not infer acceptance from release signatures, package installation, service health, or handoff readiness.

此前发布的 P64 与 W670 release 仍是针对精确源码身份严格验证过的不可变资产。Host 2238 的 Catalyst staging **PASS**，但首次 Core Begin 因 `CORE_BOOTSTRAP_SOURCE_REF_UNTRUSTED` 被阻止；原 transaction 与 guest 状态已保留，runtime source activation 未开始。Platform PR #103 已合并，正式 release 仍待完成。Catalyst runtime、Plugin invoke、启动时 binding 恢复/重启、Client/browser、SFT、Echo 和全新主机验收仍为 **PENDING / NOT_RUN**。不得仅凭发行签名、包安装、服务健康或交接就绪推断验收通过。

### Previous W2 release / 较早的 W2 发行版本

The earlier immutable W2 Native release [`native-installer-preview-8187111d7d7e6849404cd780b6bc9f23609865bb`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-8187111d7d7e6849404cd780b6bc9f23609865bb) is superseded. It predates the current source pair and is not the current installation target or acceptance evidence.

此前不可变的 W2 Native release [`native-installer-preview-8187111d7d7e6849404cd780b6bc9f23609865bb`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-8187111d7d7e6849404cd780b6bc9f23609865bb) 已被取代。它早于当前源码对，不是当前安装目标或验收证据。

The [ADR](ADR_MODULAR_DISTRIBUTION_V01.md) records the accepted architecture, exact release evidence, and current acceptance boundary.
