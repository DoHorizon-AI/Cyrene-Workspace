# Modular Distribution v0.1: Ubuntu installation / Ubuntu 安装指南

This preview targets Ubuntu 24.04 x86_64. Workspace W `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3` (PR #152) and Platform P `99aa35d75397ab01746d90d6ca078fe20dda0c1e` (PR #107) have immutable, source-bound releases. Platform API release 409660341 has 52/52 assets matched, 24 indexed manifests, and 25 strict verification receipts. Native API release 409705856 has 18/18 assets matched and passed strict verification for all nine detached asset attestations without skip or trust cache. Root's consolidated source, pins, closure, and Ubuntu 24.04 package-payload readback also passed. Fresh-host product acceptance remains PENDING. Do not use the historical W2327, Stage78 W a3/P86, or superseded W0d48 packages for this acceptance.

此预览版面向 Ubuntu 24.04 x86_64。Workspace W `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3`（PR #152）和 Platform P `99aa35d75397ab01746d90d6ca078fe20dda0c1e`（PR #107）均已有源码绑定的不可变 release。Platform API release 409660341 的 52/52 项资产匹配，24 个 index manifest 与 25 份严格验证记录通过。Native API release 409705856 的 18/18 项资产匹配；九个 detached asset attestation 均通过严格验证，未跳过、未使用信任缓存。Root 综合源码、pins、closure 与 Ubuntu 24.04 包 payload 回读也已通过。全新主机产品验收仍待完成。此次验收不要使用历史 W2327、Stage78 W a3/P86 或已被替代的 W0d48 软件包。

## Workloads and components / Workload 与组件

| Workload | Required | Recommended / optional | Runtime |
| --- | --- | --- | --- |
| Catalyst | Catalyst service, Client Control, Client Web, Runtime Maintenance SDK | Dataset Preparation, Document Parsing, Dataset Generation, Knowledge Preparation | Native signed Python bundle and supervised services |
| Echo | Echo service | Exact Match evaluator | OCI; requires Docker Engine |
| Standalone Plugin | Runtime Maintenance SDK | One explicitly selected Plugin | Native supervised service |

| Workload | 必需组件 | 推荐 / 可选 | 运行方式 |
| --- | --- | --- | --- |
| Catalyst | Catalyst service、Client Control、Client Web、Runtime Maintenance SDK | Dataset Preparation、Document Parsing、Dataset Generation、Knowledge Preparation | 原生签名 Python bundle 与受监督服务 |
| Echo | Echo service | Exact Match evaluator | OCI；需要 Docker Engine |
| 单独安装 Plugin | Runtime Maintenance SDK | 用户显式选择一个 Plugin | 原生受监督服务 |

Catalyst Plugin IDs: `cyrene-tools-dataset-preparation`, `cyrene-tools-document-parsing`, `cyrene-tools-dataset-generation`, and `cyrene-tools-knowledge-preparation`. Echo uses `cyrene-evaluation-exact-match`. Optional components can be omitted, but their capabilities must then be reported unavailable. Echo is independent; Catalyst does not require Docker.

Catalyst Plugin ID：`cyrene-tools-dataset-preparation`、`cyrene-tools-document-parsing`、`cyrene-tools-dataset-generation`、`cyrene-tools-knowledge-preparation`。Echo 使用 `cyrene-evaluation-exact-match`。可省略可选组件，但必须报告对应 capability 不可用。Echo 独立安装；Catalyst 不需要 Docker。

## Current release identities / 当前发行身份

| Item | Exact identity | Status |
| --- | --- | --- |
| Platform | `99aa35d75397ab01746d90d6ca078fe20dda0c1e` (PR #107 merge) | [Immutable release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-99aa35d75397ab01746d90d6ca078fe20dda0c1e), API 409660341; run 38137876503; 52/52 assets matched; 24 indexed manifests and 25 strict verification receipts passed |
| Plugins Official source | `48f9148e987c6e9066038db99ae0295542e32a77`; producer run 38123576893 | Five immutable packages; 80 assets and 40 attestation subjects verified |
| Signed v2 Catalog | source `18455255cdf8b87dd8cd48ceb25e5ea411a622ad`, generation 15 | `catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`; SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d`; unchanged |
| Workspace source for Native | `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3` (PR #152 merge) | Canonical source readback passed; producer run 38144970974 succeeded on the exact source |
| Native release | [Immutable release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3), API 409705856 | 18/18 assets match API IDs/digests/sizes; strict source-bound verifier passed all nine detached attestations without skip or trust cache |
| Ubuntu 24.04 amd64 DEB | API asset 630904057 | SHA-256 `d240c03a8635ebe1e78a18330400c035b898ab62e56baf9abd1a6c4985b0b6b4`; 155,107,854 bytes |
| Consolidated Root source/pin and package-payload readback | PASS | Native full-release readback SHA-256 `60781f5bce08d2b28b82fb230bff04d46b830afcec232c7ff910bc7fc0e43780`; canonical pins `release-pins-v1.json` SHA-256 `e614d27101fa818ca06f4cdb302be338783e68c789853e4804f71e35d3e4db7a`; pins/closure readback SHA-256 `09ebf1e7c335bfe49ffa84abbccfc4149153933bdc345a0ad7435225646686cf`; Ubuntu 24.04 payload readback SHA-256 `92f0d50351ea8aa70ab0f1027cc33c596d1f8a8fc3ab507dfce2bb2275327c58` |
| Release-bound clean-host acceptance | PENDING | Acceptance results will be recorded against Native API release 409705856 and this package digest; the retained guest2246 hold is not reported as recovered |

| 项目 | 精确身份 | 状态 |
| --- | --- | --- |
| Platform | `99aa35d75397ab01746d90d6ca078fe20dda0c1e`（PR #107 merge） | [不可变 release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-99aa35d75397ab01746d90d6ca078fe20dda0c1e)，API 409660341；run 38137876503；52/52 项资产匹配；24 个 index manifest 与 25 份严格验证记录通过 |
| Plugins Official 源码 | `48f9148e987c6e9066038db99ae0295542e32a77`；producer run 38123576893 | 五个不可变包；80 项资产与 40 个 attestation subject 已验证 |
| 已签名 v2 Catalog | 源码 `18455255cdf8b87dd8cd48ceb25e5ea411a622ad`，generation 15 | `catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`；SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d`；未变化 |
| Native 构建 Workspace 源码 | `b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3`（PR #152 merge） | canonical 源码回读通过；producer run 38144970974 使用该精确源码并成功 |
| Native release | [不可变 release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3)，API 409705856 | 18/18 项资产与 API 的 ID/摘要/大小匹配；九个 detached asset attestation 严格验证通过，未跳过、未使用信任缓存 |
| Ubuntu 24.04 amd64 DEB | API asset 630904057 | SHA-256 `d240c03a8635ebe1e78a18330400c035b898ab62e56baf9abd1a6c4985b0b6b4`；155,107,854 字节 |
| Root 综合源码/pins 与包 payload 回读 | 通过 | Native full-release 回读 SHA-256 `60781f5bce08d2b28b82fb230bff04d46b830afcec232c7ff910bc7fc0e43780`；canonical pins `release-pins-v1.json` SHA-256 `e614d27101fa818ca06f4cdb302be338783e68c789853e4804f71e35d3e4db7a`；pins/closure 回读 SHA-256 `09ebf1e7c335bfe49ffa84abbccfc4149153933bdc345a0ad7435225646686cf`；Ubuntu 24.04 payload 回读 SHA-256 `92f0d50351ea8aa70ab0f1027cc33c596d1f8a8fc3ab507dfce2bb2275327c58` |
| 绑定当前 release 的全新主机验收 | 待完成 | 验收结果将绑定 Native API release 409705856 与此包摘要；不把 guest2246 保留的旧 hold 报告为已恢复 |

Earlier package-only apply attempts exposed two separate Platform issues. First, `/run/cyrene-package-runtime` disappeared when the daemon stopped; PR #105 added `RuntimeDirectoryPreserve=yes`. A later attempt reached `/run/cyrene-package-runtime/control.sock`, created for `cyrene:cyrene` (UID 999, GID 988) with mode `0660`, but bootstrap's generic `mode & 0022` check rejected it. PR #106 permits only that expected socket identity and mode; the existing rejection remains for other checked paths and objects, and a live/listening socket is still rejected. No guest-side `chmod` was used. These older immutable Native/Platform pairs remain historical evidence only.

此前的 package-only apply 暴露了两个独立 Platform 问题。首先，daemon 停止时 `/run/cyrene-package-runtime` 消失；PR #105 增加 `RuntimeDirectoryPreserve=yes`。后续尝试到达服务创建的 `/run/cyrene-package-runtime/control.sock`，该 socket 属于 `cyrene:cyrene`（UID 999、GID 988），权限为 `0660`，但 bootstrap 通用 `mode & 0022` 检查将其拒绝。PR #106 只允许该预期 socket 身份与权限；其他受检查路径和对象仍保留原拒绝规则，活动/监听 socket 仍会被拒绝。修复未使用 guest 侧 `chmod`。旧的不可变 Native/Platform 发行对仍仅作为历史证据。

The retired P86/W a3 package-only apply on guest2246 later failed with `MAINTENANCE_ADMISSION_DENIED` and left an active Standard PackageOnly hold. Platform PR #107 adds a durable source heartbeat for the empty reconciliation case; Workspace PR #151 fixes the related pre-Begin generation reuse and same-held-plan replay paths. These source changes do not migrate or clear guest2246's existing hold, and the public workload protocol has no supported cancel/recovery operation for that transaction. Preserve its disk, journal, and hold; do not issue a direct Broker RPC, clear state, or start a second plan there. Guest2246 is not claimed recovered; fresh-host acceptance is tracked separately.

A separate attempt with the superseded W0d48 Native release on fresh guest2247 reached Dataset Generation but failed with `OFFLINE_CANDIDATE_IDENTITY_MISMATCH`: the offline adapter compared the outer release-manifest digest with the inner Plugin manifest digest. Workspace PR #152 fixes that binding. The replacement b9068f0 release is published and strictly verified; its guest retry and full product acceptance remain PENDING.

全新 guest2247 上另一次使用已被替代的 W0d48 Native release 的尝试运行到 Dataset Generation 时，以 `OFFLINE_CANDIDATE_IDENTITY_MISMATCH` 失败：offline adapter 将外层 release-manifest digest 与 Plugin 内层 manifest digest 比较。Workspace PR #152 修正了该绑定。替代版本 b9068f0 已发布并通过严格验证；其主机重试与完整产品验收仍待完成。

guest2246 上已退役的 P86/W a3 package-only apply 随后以 `MAINTENANCE_ADMISSION_DENIED` 失败，并留下 active Standard PackageOnly hold。Platform PR #107 为空 reconciliation 情况增加持久化 source heartbeat；Workspace PR #151 修复相关的 Begin 前 generation 重用和同一 hold 下的 plan replay 路径。这些源码修正不会迁移或清除 guest2246 已有的 hold；公开 workload protocol 也没有该事务受支持的 cancel/recovery 操作。保留其磁盘、journal 与 hold；不要直接调用 Broker RPC、清除状态或在那里启动第二个 plan。不能声称 guest2246 已恢复；全新主机验收单独记录。

## Install the current Native release / 安装当前 Native release

Use an Ubuntu 24.04 amd64 host with sudo, curl, `sha256sum`, `stat`, and GitHub CLI 2.102.0 or newer. This exact immutable release has passed release API, asset-byte, source-binding, and detached-attestation checks. The command installs the verified package; fresh-host workload and restart acceptance are still pending. Do not substitute a historical release.

使用 Ubuntu 24.04 amd64 主机，并准备 sudo、curl、`sha256sum`、`stat` 和 GitHub CLI 2.102.0 或更高版本。此确切不可变 release 已通过 release API、资产字节、源码绑定和 detached attestation 检查。以下命令安装已验证的软件包；全新主机上的 workload 与重启验收仍待完成。不要替换为历史 release。

```bash
set -euo pipefail
WORKSPACE_REPOSITORY='DoHorizon-AI/Cyrene-Workspace'
NATIVE_TAG='native-installer-preview-b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3'
NATIVE_SOURCE_SHA='b9068f0b16f4d52fc53b3ca9bffa8d5428dd43c3'
NATIVE_DEB='cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb'
NATIVE_DEB_SHA256='d240c03a8635ebe1e78a18330400c035b898ab62e56baf9abd1a6c4985b0b6b4'
NATIVE_DEB_SIZE_BYTES='155111334'
NATIVE_RELEASE_URL="https://github.com/$WORKSPACE_REPOSITORY/releases/download/$NATIVE_TAG"
RELEASE_DIR="$(mktemp -d)"
cd "$RELEASE_DIR"
curl --fail --location --silent --show-error --output "$NATIVE_DEB" "$NATIVE_RELEASE_URL/$NATIVE_DEB"
curl --fail --location --silent --show-error --output "$NATIVE_DEB.attestation.jsonl" "$NATIVE_RELEASE_URL/$NATIVE_DEB.attestation.jsonl"
test "$(stat -c '%s' "$NATIVE_DEB")" = "$NATIVE_DEB_SIZE_BYTES"
printf '%s  %s\n' "$NATIVE_DEB_SHA256" "$NATIVE_DEB" | sha256sum --check --strict
gh attestation verify "$NATIVE_DEB" \
  --bundle "$NATIVE_DEB.attestation.jsonl" \
  --repo "$WORKSPACE_REPOSITORY" \
  --signer-workflow "$WORKSPACE_REPOSITORY/.github/workflows/native-installer-release.yml" \
  --source-ref refs/heads/develop \
  --source-digest "$NATIVE_SOURCE_SHA" \
  --predicate-type https://slsa.dev/provenance/v1
sudo apt install "./$NATIVE_DEB"
```

After the exact PR #152 Native DEB is published, verified, and installed, install Catalyst with the signed preview Catalog:

安装确切的 PR #152 Native DEB 后，使用签名 preview Catalog 安装 Catalyst：

```bash
sudo cyrene workload install catalyst --channel preview --yes
```

The `yes` option confirms that invocation's resolved plan. It does not skip source, signature, or digest checks. Review the plan and stop on any blocker. Keep the plan identity, receipts, error details, and transaction journal if an operation is interrupted.

`yes` 选项只确认本次解析出的 plan，不会跳过源码、签名或摘要检查。检查 plan 并在遇到 blocker 时停止。操作中断时保留 plan identity、receipt、错误详情和 transaction journal。

Read-only status:

只读状态查询：

    printf '%s\n' '{"protocolVersion":"cyrene.workload-plan.v1","operation":"status","workloadId":"catalyst"}' | sudo cyrene workload --json | jq .

## Install one Plugin / 单独安装 Plugin

Use the `plugins` workload with one explicit component selection. Example check for Document Parsing:

通过 `plugins` workload 显式选择一个组件。以下示例检查 Document Parsing：

    sudo cyrene workload --json <<'JSON' | jq .
    {"protocolVersion":"cyrene.workload-plan.v1","operation":"check","workloadId":"plugins","channel":"preview","targetId":"linux-ubuntu-24.04-x86_64","action":"install","selections":{"includeComponentIds":["cyrene-tools-document-parsing"],"excludeComponentIds":[],"choices":{}}}
    JSON

Use the returned `planId` and `planDigest` unchanged for stage and apply. Empty selection is blocked; it does not install every Plugin.

stage 与 apply 使用返回的 `planId` 和 `planDigest`，保持其值不变。空选择会被拒绝，不会安装全部 Plugin。

## Add or remove Echo / 增装或移除 Echo

Echo is an optional independent workload and requires Docker Engine. Keep the daemon active and the standard socket available to the privileged workload operation. Do not add users to the `docker` group or loosen socket permissions.

Echo 是独立可选 workload，需要 Docker Engine。保持 daemon 运行，并让特权 workload 操作可访问标准 socket。不要把用户加入 `docker` 组或放宽 socket 权限。

    sudo cyrene workload install echo --channel preview --yes

Remove only Echo with its workload plan:

使用其 workload plan 只移除 Echo：

    sudo cyrene workload uninstall cyrene-echo --workload echo --channel preview --yes

The default lifecycle retains Catalyst and user datasets, artifacts, review history, and backups when Echo is removed. Retention is not a backup guarantee.

默认移除 Echo 时会保留 Catalyst 与用户 Dataset、Artifact、审核历史和备份；保留机制不能替代独立备份。

## Access and limits / 访问与限制

Keep Client and Control bound to `127.0.0.1`. The expected local Client address is `http://127.0.0.1:8100/`; browser access for this release is PENDING. For remote access, use an approved SSH local forward and keep the service bound to loopback:

Client 与 Control 仅绑定到 `127.0.0.1`。预期本机 Client 地址为 `http://127.0.0.1:8100/`；此 release 的浏览器访问验收待完成。远程访问使用获准的 SSH 本地转发，服务仍保持 loopback 绑定：

    ssh -L 8100:127.0.0.1:8100 user@ubuntu-host

This preview supports Ubuntu 24.04 x86_64 only. Release signatures and successful package installation do not prove Plugin invocation, data processing, SFT loading, Client use, restart recovery, or Echo evaluation. Tie each result to exact release identities; mark unrun checks `NOT_RUN`.

此预览版仅支持 Ubuntu 24.04 x86_64。发行签名与包安装成功不能证明 Plugin 调用、数据处理、SFT 加载、Client 使用、重启恢复或 Echo 评测通过。每项结果都应绑定精确发行身份；未运行的检查标记为 `NOT_RUN`。

The preview has no customer-facing arbitrary-version downgrade command. On failure, preserve receipts and the transaction journal and use the supported maintenance path. Uninstall preserves user datasets, artifacts, review history, and backups by default; keep separate backups.

预览版没有面向客户的任意版本降级命令。发生故障时保留 receipt 与 transaction journal，并使用受支持的 maintenance 流程。默认卸载会保留用户 Dataset、Artifact、审核历史和备份；仍需单独保留备份。

See the [architecture decision record](ADR_MODULAR_DISTRIBUTION_V01.md) for system boundaries and release evidence.

系统边界与发行证据见[架构决策记录](ADR_MODULAR_DISTRIBUTION_V01.md)。
