# Modular Distribution v0.1: Ubuntu installation / Ubuntu 安装指南

This preview targets Ubuntu 24.04 x86_64. The immutable Native release for Workspace W2327, Platform PR #106's release, and five source-bound Plugin packages are published and strictly verified. The clean-host acceptance is still in progress; release verification does not prove runtime use. Use only the exact Native release and commands below, and do not reuse an older Native/Platform pair.

此预览版面向 Ubuntu 24.04 x86_64。Workspace W2327 对应的不可变 Native release、Platform PR #106 的 release，以及五个源码绑定的 Plugin 包均已发布并通过严格验证。全新主机验收仍在进行；发行验证不等于运行验收。只使用下方列出的确切 Native release，不要复用旧的 Native/Platform 发行对。

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
| Platform PR #106 source and release | `86ee31721113d548d0dccfa88bc7e080df9cd1f9`; [release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-86ee31721113d548d0dccfa88bc7e080df9cd1f9), API 409542106, run 38123069265 | Immutable; all 52 assets match API IDs/digests/sizes; 24 indexed manifests and 31 subjects passed strict verification |
| Plugins Official source | `48f9148e987c6e9066038db99ae0295542e32a77`; producer run 38123576893 | Five immutable packages; 80 assets and 40 attestation subjects verified |
| Workspace source for Native | `2327cf9221a23721b64017f326b5ba47cf2bc3fd` | Exact source for Native producer run 38125742126, attempt 1 |
| Native release | [immutable release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd), API 409550498 | All 18 asset identities/digests/sizes match the official API; strict no-skip/no-trust-cache verification passed, including nine detached asset attestations |
| Ubuntu 24.04 amd64 DEB | [Download](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd/cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb) | SHA-256 `d9da6f3cd9c72f55e6d27e7ff4219f5acdb77c2699504ef3df153393b628b1c9`; 155,116,908 bytes; API asset 630267360 |
| Clean-host acceptance | IN PROGRESS | Host initialization, preflight, DEB installation and static package readback passed; the first Catalyst stage was blocked by a retryable Plugin-resolution NETWORK_ERROR; the plan was preserved and apply has not run |

| 项目 | 精确身份 | 状态 |
| --- | --- | --- |
| Platform PR #106 源码与 release | `86ee31721113d548d0dccfa88bc7e080df9cd1f9`；[release](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-86ee31721113d548d0dccfa88bc7e080df9cd1f9)，API 409542106、run 38123069265 | 不可变；52 项资产的 API ID/摘要/大小均匹配；24 个 index manifest 与 31 个 subject 严格验证通过 |
| Plugins Official 源码 | `48f9148e987c6e9066038db99ae0295542e32a77`；producer run 38123576893 | 五个不可变包；80 项资产与 40 个 attestation subject 已验证 |
| Native 构建 Workspace 源码 | `2327cf9221a23721b64017f326b5ba47cf2bc3fd` | Native producer run 38125742126、attempt 1 的精确源码 |
| Native release | [不可变 release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd)，API 409550498 | 18 项资产身份、摘要、大小与官方 API 匹配；9 个 detached asset attestation 严格验证通过，未跳过、未使用信任缓存 |
| Ubuntu 24.04 amd64 DEB | [下载](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd/cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb) | SHA-256 `d9da6f3cd9c72f55e6d27e7ff4219f5acdb77c2699504ef3df153393b628b1c9`；155,116,908 字节；API asset 630267360 |
| 全新主机验收 | 进行中 | 主机初始化、preflight、DEB 安装和静态包回读已通过；首次 Catalyst stage 被可重试的 Plugin 解析 NETWORK_ERROR 阻止；原计划已保留，尚未执行 apply |

Earlier package-only apply attempts exposed two separate Platform issues. First, `/run/cyrene-package-runtime` disappeared when the daemon stopped; PR #105 added `RuntimeDirectoryPreserve=yes`. A later attempt reached `/run/cyrene-package-runtime/control.sock`, created for `cyrene:cyrene` (UID 999, GID 988) with mode `0660`, but bootstrap's generic `mode & 0022` check rejected it. PR #106 permits only that expected socket identity and mode; the existing rejection remains for other checked paths and objects, and a live/listening socket is still rejected. No guest-side `chmod` was used. These older immutable Native/Platform pairs remain historical evidence only.

此前的 package-only apply 暴露了两个独立 Platform 问题。首先，daemon 停止时 `/run/cyrene-package-runtime` 消失；PR #105 增加 `RuntimeDirectoryPreserve=yes`。后续尝试到达服务创建的 `/run/cyrene-package-runtime/control.sock`，该 socket 属于 `cyrene:cyrene`（UID 999、GID 988），权限为 `0660`，但 bootstrap 通用 `mode & 0022` 检查将其拒绝。PR #106 只允许该预期 socket 身份与权限；其他受检查路径和对象仍保留原拒绝规则，活动/监听 socket 仍会被拒绝。修复未使用 guest 侧 `chmod`。旧的不可变 Native/Platform 发行对仍仅作为历史证据。

## Install the verified Native release / 安装已验证的 Native release

Use an Ubuntu 24.04 amd64 host with sudo, curl, `sha256sum`, `stat`, and GitHub CLI 2.102.0 or newer. The commands below download the exact immutable DEB and its detached attestation, check the published size and SHA-256, verify the signer workflow and source commit, then install the package. Stop if any check fails.

使用 Ubuntu 24.04 amd64 主机，并准备 sudo、curl、`sha256sum`、`stat` 和 GitHub CLI 2.102.0 或更高版本。以下命令下载确切不可变 DEB 及 detached attestation，核对已发布的文件大小和 SHA-256，再验证签名 workflow 与 source commit，最后安装软件包。任何检查失败都应停止。

```bash
set -euo pipefail
WORKSPACE_REPOSITORY='DoHorizon-AI/Cyrene-Workspace'
NATIVE_TAG='native-installer-preview-2327cf9221a23721b64017f326b5ba47cf2bc3fd'
NATIVE_SOURCE_SHA='2327cf9221a23721b64017f326b5ba47cf2bc3fd'
NATIVE_DEB='cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb'
NATIVE_DEB_SHA256='d9da6f3cd9c72f55e6d27e7ff4219f5acdb77c2699504ef3df153393b628b1c9'
NATIVE_DEB_SIZE_BYTES='155116908'
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

After the verified DEB is installed, install Catalyst with the signed preview Catalog:

验证并安装 DEB 后，使用签名 preview Catalog 安装 Catalyst：

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

Keep Client and Control bound to `127.0.0.1`. The expected local Client address is `http://127.0.0.1:8100/`; first accepted access is PENDING. For remote access, use an approved SSH local forward and keep the service bound to loopback:

Client 与 Control 仅绑定到 `127.0.0.1`。预期本机 Client 地址为 `http://127.0.0.1:8100/`；首次访问验收待完成。远程访问使用获准的 SSH 本地转发，服务仍保持 loopback 绑定：

    ssh -L 8100:127.0.0.1:8100 user@ubuntu-host

This preview supports Ubuntu 24.04 x86_64 only. Release signatures and successful package installation do not prove Plugin invocation, data processing, SFT loading, Client use, restart recovery, or Echo evaluation. Tie each result to exact release identities; mark unrun checks `NOT_RUN`.

此预览版仅支持 Ubuntu 24.04 x86_64。发行签名与包安装成功不能证明 Plugin 调用、数据处理、SFT 加载、Client 使用、重启恢复或 Echo 评测通过。每项结果都应绑定精确发行身份；未运行的检查标记为 `NOT_RUN`。

The preview has no customer-facing arbitrary-version downgrade command. On failure, preserve receipts and the transaction journal and use the supported maintenance path. Uninstall preserves user datasets, artifacts, review history, and backups by default; keep separate backups.

预览版没有面向客户的任意版本降级命令。发生故障时保留 receipt 与 transaction journal，并使用受支持的 maintenance 流程。默认卸载会保留用户 Dataset、Artifact、审核历史和备份；仍需单独保留备份。

See the [architecture decision record](ADR_MODULAR_DISTRIBUTION_V01.md) for system boundaries and release evidence.

系统边界与发行证据见[架构决策记录](ADR_MODULAR_DISTRIBUTION_V01.md)。
