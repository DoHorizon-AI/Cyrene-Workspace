# Modular Distribution v0.1: Ubuntu installation / Ubuntu 安装指南

**Status:** The immutable Ubuntu 24.04 amd64 preview from Workspace source `0e8d8f56cc3bb827320a85671fff43220d0a4551` is published and independently verified. The official release run succeeded in all four jobs, and strict release, root asset, and release-pin readbacks passed. A fresh-host acceptance handoff targets guest 2234, but a complete authoritative phase readback is not yet recorded. No Catalyst/Core, Client, SFT-consumer, or Echo acceptance is claimed. See the [ADR](ADR_MODULAR_DISTRIBUTION_V01.md) for exact evidence.

**状态：** Workspace 源码 `0e8d8f56cc3bb827320a85671fff43220d0a4551` 对应的不可变 Ubuntu 24.04 amd64 预览版已发布并通过独立发行物验证。官方 release run 四个 job 全部成功，严格发行物、root 资产和 release-pin 回读均通过。全新主机 2234 已收到验收交接，但尚未记录完整且权威的阶段回读。本指南不宣称 Catalyst/Core、Client、SFT consumer 或 Echo 验收通过。确切证据见 [ADR](ADR_MODULAR_DISTRIBUTION_V01.md)。

## Start here / 快速开始

Use a stock Ubuntu 24.04 x86_64 host, an administrator account with `sudo`, and an internet connection. Echo is optional and requires Docker; Catalyst does not.

使用标准 Ubuntu 24.04 x86_64 主机、具备 `sudo` 的管理员账户及网络连接。Echo 为可选项且需要 Docker；Catalyst 不需要 Docker。

| Item | Official preview identity |
| --- | --- |
| Native release | [`native-installer-preview-0e8d8f56cc3bb827320a85671fff43220d0a4551`](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-0e8d8f56cc3bb827320a85671fff43220d0a4551) |
| Producer | [Run 38034438591](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/38034438591), attempt 2; all four jobs succeeded; release API ID `408779397` |
| Workspace source | `0e8d8f56cc3bb827320a85671fff43220d0a4551` (`refs/heads/develop`; PR #135 merge) |
| Ubuntu 24.04 amd64 DEB | SHA-256 `ddb6548d824fd6cdc40ca3635e43e60a648bd43100661f4f5e0d26124d7b2280` (155,106,734 bytes) |
| Release manifest / source receipt | `native-installer-release-v2.json`: `85a0153696140d54ebb9c5c552ffc7434fa92414c942ed7b5a56c59768e481ca`; `native-installer-source-receipt-v2.json`: `ba5d91023be13429f57c298baf7c92cad88b3399b4d2bd3f4b66bc9bd531a8ce` |
| Active Catalog v2 | `catalog-v2-preview-18455255cdf8b87dd8cd48ceb25e5ea411a622ad`, generation 15, source `18455255cdf8b87dd8cd48ceb25e5ea411a622ad`, SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d`, attestation bundle SHA-256 `37762cbdabe99c5e62fa9494531ff7e16962b241d2cff47bf920530a33585e85` |
| Independent release verification | Strict full-envelope verification passed; root independent asset/source readback SHA-256 `a8e0396cc98cea109f0c7fd736ac41b2dc2586468afb6e02dde8ede0762a3f0f` |
| Frozen component identities | Platform `preview-daabf9ff561b4ab9296094d8198fd1fa3418b32b`; Catalyst `preview-4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b`; Plugins Official source `443da5e071c9d7e82e7f28788cc04777e0bea1b9`; Client Web `preview-cyrene-client-workspace-web-b3c3f964540e3aa761612371891e3bb6a3f7ad59` |
| Fresh-host acceptance | Ubuntu 24.04 guest 2234; handoff authorized. Complete authoritative phase results are pending and are not implied by release verification. |
| Client static Web | `preview-cyrene-client-workspace-web-b3c3f964540e3aa761612371891e3bb6a3f7ad59` (static payload; not a server or host-acceptance result) |

The published envelope is immutable and passed strict verification for all 18 assets, 21 bound tuples, seven verifier source blobs, and 20 evidence records with no skipped checks. Independent root readback and the exact release-pin source/schema checks also passed. Fresh-host acceptance is a separate gate; final authoritative phase readback remains pending.

已发布发行包不可变；严格验证通过全部 18 项资产、21 个绑定 tuple、7 个 verifier 源文件及 20 条 evidence，且未跳过检查。独立 root 回读和精确 release-pin source/schema 校验也通过。全新主机验收是独立门禁；最终权威阶段回读仍待完成。


The verified CLI accepts an optional `--channel`; the signed active Catalog defaults to `stable`, while this preview uses `preview`. After installing the exact final release below, run this command to install Catalyst:

已验证 CLI 支持可选 `--channel`；签名 active Catalog 默认使用 `stable`，本预览版使用 `preview`。安装下方固定的最终发行包后，运行此命令安装 Catalyst：

```bash
sudo cyrene workload install catalyst --channel preview --yes
```

Only after Catalyst and Client activation should the user open `http://127.0.0.1:8100/` on the Ubuntu host. For remote use, replace `user@ubuntu-host` with the actual account and host, create an approved SSH local forward, and open the same address locally:

只有 Catalyst 和 Client 激活后，才在 Ubuntu 主机打开 `http://127.0.0.1:8100/`。远程访问时，将 `user@ubuntu-host` 替换为实际账户和主机，通过获准的 SSH 本地转发连接，再在本机浏览器打开相同地址：

```bash
ssh -L 8100:127.0.0.1:8100 user@ubuntu-host
```

Keep the service bound to loopback. Do not expose the Client or Control listener on `0.0.0.0`; do not put installation tokens in shell commands.

服务应保持 loopback 绑定。不要将 Client 或 Control listener 暴露到 `0.0.0.0`，也不要把安装 token 写入 shell 命令。

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

## 1. Install the final verified preview / 安装最终验证的预览版

The values below pin the immutable 0e8d preview and its active-v2 Catalog. Independent release and root readbacks verify the published package identities; the fresh-host acceptance result is still pending. Keep those two claims separate.

下列值固定不可变的 0e8d 预览版及其 active-v2 Catalog。独立发行物与 root 回读确认了已发布包的身份；全新主机验收结果仍待返回。两类证据应分别表述。

Use an Ubuntu 24.04 amd64 host. The values below pin the published native
release and the active-v2 Catalog recorded in its verified source receipt. Do
not substitute a `latest` release or change an identity without verifying its
exact immutable release and detached attestations.

使用 Ubuntu 24.04 amd64 主机。下列值固定了已发布的 native release，以及其已验签 source receipt 记录的 active-v2
Catalog。不得用 `latest` 代替，也不得在未验证确切不可变发行物和 detached attestation 前修改身份：

```bash
WORKSPACE_REPOSITORY='DoHorizon-AI/Cyrene-Workspace'
NATIVE_RELEASE_ID='native-installer-preview-0e8d8f56cc3bb827320a85671fff43220d0a4551'
NATIVE_SOURCE_REF='refs/heads/develop'
NATIVE_SOURCE_SHA='0e8d8f56cc3bb827320a85671fff43220d0a4551'
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
  'ddb6548d824fd6cdc40ca3635e43e60a648bd43100661f4f5e0d26124d7b2280' "$NATIVE_DEB_ASSET" \
  '85a0153696140d54ebb9c5c552ffc7434fa92414c942ed7b5a56c59768e481ca' "$NATIVE_MANIFEST_ASSET" \
  'ba5d91023be13429f57c298baf7c92cad88b3399b4d2bd3f4b66bc9bd531a8ce' "$NATIVE_SOURCE_RECEIPT_ASSET" \
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

On a fresh host, the normal checked Catalyst workload plan also performs the
official first-Core bootstrap during `apply`: it initializes the missing
activity-source catalog and starts the required Core components from the exact
verified plan. Do not manually import or create the activity-source catalog,
run a separate Core recovery command, or set `PYTHONPATH`. Review the check and
staged component identities; if bootstrap reports a blocker, keep the receipt
and error details and stop rather than editing runtime state by hand.

The formal handoff authorizes acceptance on fresh Ubuntu guest 2234. A complete authoritative phase readback is still pending. Preserve the plan and receipts; do not report Core, Product, Client, SFT-consumer, or Echo acceptance until the corresponding evidence is available. Do not edit Broker state or infer workload success from signed package verification.

正式交接已授权在全新 Ubuntu 主机 2234 上验收，但完整且权威的阶段回读仍待取得。保留 plan 和 receipts；在取得对应证据前，不要报告 Core、Product、Client、SFT consumer 或 Echo 验收通过。不要手工修改 Broker 状态，也不要把签名包验证当作 workload 成功。

To inspect an already installed workload, use the read-only status request:

若需检查已安装 workload，使用以下只读 status 请求：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"status","workloadId":"catalyst"}
JSON
```

Required components cannot be excluded. If a required component is excluded,
`check` returns a blocker and no plan may proceed to stage or apply. Recommended
components are selected by default and may be explicitly excluded in `check`.
For example, to omit only Document Parsing, check with `channel` set to
`preview`; use that same value in the resulting stage and apply requests:

必需组件不能取消；若排除必需组件，`check` 会返回 blocker，计划不得进入 stage 或 apply。推荐组件默认纳入，
可在 `check` 中显式排除。以下示例只取消 Document Parsing，并将 channel 设为 `preview`；之后的 stage/apply 请求也要
使用相同值：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"check","workloadId":"catalyst","channel":"preview","targetId":"linux-ubuntu-24.04-x86_64","action":"install","selections":{"includeComponentIds":[],"excludeComponentIds":["cyrene-tools-document-parsing"],"choices":{}}}
JSON
```

Proceed only when the returned `result.status` is `ready` and the selected rows
match the intended choices. Copy that response's exact `planId`, `planDigest`,
and `channel` into the stage request:

只有在返回的 `result.status` 为 `ready` 且所选组件符合预期时才继续。将该响应中的精确 `planId` 与
`planDigest` 填入 stage 请求，并保留相同的 `channel`：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"stage","workloadId":"catalyst","channel":"preview","targetId":"linux-ubuntu-24.04-x86_64","action":"install","planId":"<planId-from-check>","planDigest":"sha256:<64-lowercase-hex-from-check>"}
JSON
```

Review the staged rows and only then apply the same action, channel, plan ID,
and digest:

检查 stage 返回的组件行；确认后才用相同 action、channel、plan ID 和 digest 执行 apply：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"apply","workloadId":"catalyst","channel":"preview","targetId":"linux-ubuntu-24.04-x86_64","action":"install","planId":"<same-planId>","planDigest":"sha256:<same-64-hex>","confirmation":{"planId":"<same-planId>","planDigest":"sha256:<same-64-hex>","confirmed":true}}
JSON
```

An omitted plugin remains a missing capability; do not claim its feature is
available or silently substitute another package. Use the capability response
from the Product/Client API to explain which optional function is unavailable.

未安装的插件仍表示对应 capability 缺失；不得宣称其功能可用，也不得静默替换其他包。应依据 Product/Client
API 的 capability 响应说明具体不可用的可选功能。

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
Echo requires the official [Docker Engine for Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
with an active daemon and the standard Unix socket available to the privileged
workload operation. Check the host before selecting Echo:

Echo 是单独的可选 workload，其 Linux OCI 预览发行物见上表。Catalyst 使用原生 bundle，**不需要 Docker**。
选择 Echo 需要安装官方 [Ubuntu Docker Engine](https://docs.docker.com/engine/install/ubuntu/)，并确保 daemon 正在
运行，且特权 workload 操作可访问标准 Unix socket。选择 Echo 前检查主机：

```bash
sudo systemctl is-active docker
sudo docker info
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
channel for stage and apply. The release and signed Catalog path are available;
Final evidence for Echo installation, evaluation, and lifecycle acceptance has not been recorded.

若不安装 evaluator，使用相同的 check/stage/apply 协议并设置 workload `echo`、`channel: "preview"` 与
`excludeComponentIds: ["cyrene-evaluation-exact-match"]`；stage 和 apply 重复相同 channel。发行物和签名 Catalog
路径已就绪；Echo 安装、评测与 lifecycle 尚无可核验的最终验收结果。

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

### Acceptance boundary and host limitations / 验收边界与主机限制

The official 0e8 release and its signatures are verified. Fresh-host acceptance was handed off on 2234, but a complete authoritative phase readback is not yet recorded here. Do not describe Core startup, Client access, SFT export/independent loading, or Echo lifecycle as passed until the corresponding receipts have been read back. See the ADR for the current acceptance boundary.

官方 0e8 发行版及签名已验证。2234 全新主机验收已完成交接，但本指南尚未记录完整且权威的阶段回读。只有回读相应 receipt 后，才能报告 Core 启动、Client 访问、SFT 导出/独立加载或 Echo lifecycle 通过。当前验收边界见 ADR。

The resolver currently uses the public GitHub API without a supported authenticated download path. A temporary API quota limit can return retryable `NETWORK_ERROR` before stage/apply; keep the plan and receipts, wait for the quota window to reset, then retry. Do not inject `GH_TOKEN` or edit local runtime state.

当前 resolver 通过公开 GitHub API 获取元数据，尚无受支持的认证下载入口。临时 API 配额限制可能在 stage/apply 前返回可重试的 `NETWORK_ERROR`；保留 plan 和 receipt，等待配额窗口恢复后重试。不要注入 `GH_TOKEN` 或手工修改本机 runtime 状态。

The [ADR](ADR_MODULAR_DISTRIBUTION_V01.md) records the accepted architecture, exact release evidence, and current acceptance boundary.
