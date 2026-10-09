# Modular Distribution v0.1: Ubuntu installation / Ubuntu 安装指南

**Status:** operator instructions for the modular-distribution candidate.
Component preview releases are now published, but they are prereleases and are
not a substitute for the matching generation-15 native DEB, source receipt, and
Catalog binding. The current Workspace native candidate is still being
produced; its fresh-host release acceptance is **NOT_RUN**. A successful local
check, a staged DEB, or an older release does not change that status.

**状态：** 本文说明模块化发行候选版的操作步骤。组件 preview release 已发布，但它们仍是预发布版本，不能替代
匹配的 generation-15 native DEB、source receipt 和 Catalog binding。当前 Workspace native 候选版本仍在构建；全新主机
发行验收为 **NOT_RUN**。本地检查通过、DEB 已暂存或旧版发行物均不能改变此状态。

Published component preview refs / 已发布的组件预览版本：

| Component | Published preview release |
| --- | --- |
| Client Control and Web | [Control `b3c3f96`](https://github.com/DoHorizon-AI/Cyrene-Client/releases/tag/preview-cyrene-client-workspace-control-b3c3f964540e3aa761612371891e3bb6a3f7ad59); [Web `b3c3f96`](https://github.com/DoHorizon-AI/Cyrene-Client/releases/tag/preview-cyrene-client-workspace-web-b3c3f964540e3aa761612371891e3bb6a3f7ad59) |
| Native Platform base components | [Platform `1ec629e`](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-1ec629e55195fa803651fb23d1c50d04696a94a8) |
| Runtime Maintenance SDK (default preview tuple) | [Platform SDK `ddf142f`](https://github.com/DoHorizon-AI/Cyrene-Platform/releases/tag/preview-ddf142f553e322b664689c2be6df41152ded92d5); archive `cyrene-runtime-maintenance-sdk-linux-ubuntu-24.04-x86_64-python-3.12-library.tar.gz`, SHA-256 `584e178cc416f15a609189ca346b21ac8675a0f64176a0f346dab15630ed5e91` (23,419 bytes) |
| Catalyst native bundle | [Catalyst `4ad9506`](https://github.com/DoHorizon-AI/Cyrene-Catalyst/releases/tag/preview-4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b) |
| Echo Linux OCI manifest | [Echo `d5a9200`](https://github.com/DoHorizon-AI/Cyrene-Echo/releases/tag/preview-d5a920078077bdbaa76e7117ad83f69cbfa1c614) |
| Five plugin packages | [Dataset Preparation](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-dataset-preparation-0.2.0-443da5e071c9d7e82e7f28788cc04777e0bea1b9); [Document Parsing](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-document-parsing-0.2.0-443da5e071c9d7e82e7f28788cc04777e0bea1b9); [Dataset Generation](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-dataset-generation-0.2.0-443da5e071c9d7e82e7f28788cc04777e0bea1b9); [Knowledge Preparation](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-tools-knowledge-preparation-0.1.0-443da5e071c9d7e82e7f28788cc04777e0bea1b9); [Exact Match](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/releases/tag/preview-cyrene-evaluation-exact-match-0.1.0-443da5e071c9d7e82e7f28788cc04777e0bea1b9) |

The current Workspace native release candidate targets source commit
`a894d4bfce2bedba94b0d6449b5044dc2f982b9d` on `refs/heads/develop`, with
expected tag `native-installer-preview-a894d4bfce2bedba94b0d6449b5044dc2f982b9d`.
Its [producer run](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/37865468304)
has not yet published the immutable release. This is candidate metadata only:
the DEB digest, source receipt, Catalog release identity, and Catalog digest
remain unset. Do not use this candidate in the install commands below.

当前 Workspace native 发行候选版本以 `refs/heads/develop` 上的源码提交
`a894d4bfce2bedba94b0d6449b5044dc2f982b9d` 为目标，预期 tag 为
`native-installer-preview-a894d4bfce2bedba94b0d6449b5044dc2f982b9d`。其
[producer run](https://github.com/DoHorizon-AI/Cyrene-Workspace/actions/runs/37865468304)
尚未发布不可变发行物。这些信息仅为候选值：DEB digest、source receipt、Catalog release identity 与 Catalog digest
仍未确定。不要将此候选值用于下方安装命令。

These refs are previews, not a claim that the native release has resolved,
verified, and bound these exact assets. Use only identities in the completed
native release receipt and signed Catalog. Anonymous detached-bundle provenance
verification for the DDF SDK tuple passed; installation on the target runtime
host remains `NOT_RUN`.

上述版本均为预览版，不能据此声称 native release 已解析、验签并绑定这些确切资产。DDF SDK tuple 的匿名 detached bundle
provenance 验证已通过；目标 runtime host 上的安装仍为 `NOT_RUN`。仅使用已完成的 native release receipt 和签名
Catalog 中列出的身份。

The signed Catalog currently defaults to `stable`; the component refs above are
`preview` releases. To select these refs, pass `preview` explicitly. The optional
`channel` on `check` otherwise follows `Catalog.defaultChannel`; the chosen
channel is bound into the plan, so repeat it unchanged for `stage` and `apply`.
The read-only `status` operation does not take a channel.

当前签名 Catalog 的默认通道为 `stable`；上表组件版本属于 `preview`。选择这些版本时必须显式传入 `preview`。
`check` 的可选 `channel` 若省略，则遵循 `Catalog.defaultChannel`；选定通道会绑定在计划中，因此 `stage` 和
`apply` 必须使用同一通道。只读 `status` 操作不接收 channel。

The Client static Web archive is published as
`preview-cyrene-client-workspace-web-b3c3f964540e3aa761612371891e3bb6a3f7ad59`.
It is a static payload, not a server or proof of full Workspace installation.
Workspace serves the activated `current` tree through its managed loopback Nginx
host at `http://127.0.0.1:8100/`; the host proxies same-origin Control requests
to `127.0.0.1:5182`. Do not expose either listener on a public interface. Use an
existing approved SSH local forward when accessing the host remotely; installation
preserves existing SSH-forwarding configuration by default.

Client 静态 Web 包已发布为
`preview-cyrene-client-workspace-web-b3c3f964540e3aa761612371891e3bb6a3f7ad59`，但它只是静态内容，
不是 HTTP 服务，也不能证明 Workspace 安装已完成。Workspace 通过受管的 loopback Nginx 服务
`http://127.0.0.1:8100/`，并将同源 Control 请求转发至 `127.0.0.1:5182`。不要把任一 listener 暴露到公网；
远程访问使用现有且获准的 SSH 本地转发。安装默认保留已有 SSH 转发配置。

### Host prerequisites / 主机前置条件

Use Ubuntu 24.04 amd64. The following host preparation installs only the
required transfer and JSON tools, then installs or upgrades `gh` from GitHub's
official Debian/Ubuntu APT keyring and `signed-by` source if it is absent or
below the minimum. An existing `gh` that already meets the minimum is left
unchanged. The APT command targets `gh`; this block does not run a system
upgrade. The minimum is GitHub CLI **2.102.0**; the version gate and help check
below fail before any release download if it is still too old or lacks
attestation support.

使用 Ubuntu 24.04 amd64。以下主机准备命令安装必需的下载和 JSON 工具；若 `gh` 缺失或低于最低版本，则通过
GitHub 官方 Debian/Ubuntu APT keyring 与 `signed-by` 源安装或升级。已有版本满足最低要求时不作更改。APT 命令只针对
`gh`，此处不执行系统升级。最低版本为 GitHub CLI **2.102.0**；若最终版本仍过旧或不支持 attestation，版本门禁和
help 检查会在下载发行物前失败。

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

## 1. Verify and install the native DEB / 验签并安装 native DEB

Use an Ubuntu 24.04 amd64 host. Obtain release identifiers from the exact signed
Workspace Catalog v2/native release pins; never substitute a `latest` release.
The following values are deliberate placeholders until the matching release is
published:

使用 Ubuntu 24.04 amd64 主机。发行身份须来自精确签名的 Workspace Catalog v2/native release pins；
不得用 `latest` 代替。匹配发行物发布前，下列值保持为明确占位符：

```bash
WORKSPACE_REPOSITORY='DoHorizon-AI/Cyrene-Workspace'
NATIVE_RELEASE_ID='native-installer-preview-<40-hex-source-sha>'
NATIVE_SOURCE_REF='refs/heads/develop'
NATIVE_SOURCE_SHA='<same-40-hex-source-sha>'
NATIVE_DEB_ASSET='cyrene_<version>_ubuntu-24.04_amd64.deb'
NATIVE_MANIFEST_ASSET='native-installer-release-v2.json'
NATIVE_SOURCE_RECEIPT_ASSET='native-installer-source-receipt-v2.json'
CATALOG_RELEASE_ID='catalog-v2-preview-<40-hex-catalog-source-sha>'
CATALOG_SOURCE_REF='refs/heads/develop'
CATALOG_SOURCE_SHA='<catalog-release-source-sha-from-native-receipt>'
RELEASE_DIR="$(mktemp -d)"
```

Do not run these commands while any value is still a placeholder. Download the
exact public GitHub release assets over HTTPS; no repository clone, source
build, or GitHub login is needed. First verify the detached attestation for
`SHA256SUMS`, then check the downloaded payload checksums. Verify the DEB and v2
release manifest against the exact repository, publisher workflow, source ref,
and source SHA.
Verify the v2 source receipt's active-v2 catalog release ID and source identity,
then verify its exact catalog bytes, generation, and detached attestation. Stop
if any identity differs or if the release lacks the v2 manifest, source receipt,
DEB/catalog attestation bundles, or expected generation-15 catalog binding.

占位符未替换前不要运行以下命令。通过 HTTPS 下载确切的公开 GitHub release assets，无需 clone 仓库、构建源码或登录 GitHub。
先验签 `SHA256SUMS` 的 detached attestation，再校验下载的 payload checksum。然后按确切仓库、发布 workflow、source ref 和 source SHA 验证 DEB 与 v2
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

Use only the DEB for Ubuntu 24.04 amd64. Keep the release tag, source SHA,
manifest, checksum output, and attestation verification result with the test
record. The DEB-installed helper and its trusted Catalog—not a source checkout
or manually copied catalog—resolve component release indexes, manifests,
artifacts, and attestations before staging.

只安装 Ubuntu 24.04 amd64 对应的 DEB。测试记录保留 release tag、source SHA、manifest、checksum
输出和 attestation 验证结果。组件 release index、manifest、artifact 和 attestation 由 DEB 安装的
可信 helper 与 Catalog 解析；不得用源码 checkout 或手工复制的 Catalog 代替。

## 2. Inspect and install Catalyst / 检查并安装 Catalyst

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

```bash
sudo cyrene workload install catalyst --channel preview --yes
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

Once the matching native release and signed Catalog pin Echo's exact OCI
artifact, select the `preview` channel to add Echo and its default recommended
exact-match evaluator without replacing or uninstalling Catalyst:

匹配的 native release 和签名 Catalog 固定 Echo 确切 OCI artifact 后，显式选择 `preview` 通道即可增装 Echo 及默认
推荐的 exact-match evaluator；该命令不会替换或卸载 Catalyst：

```bash
sudo cyrene workload install echo --channel preview --yes
```

To omit the evaluator, use the same check/stage/apply protocol with workload
`echo`, `channel: "preview"`, and
`excludeComponentIds: ["cyrene-evaluation-exact-match"]`; repeat the same
channel for stage and apply. Until the matching native release and signed
Catalog binding are available, this path is **PENDING_RELEASE** and must not be
reported as installed or accepted.

若不安装 evaluator，使用相同的 check/stage/apply 协议并设置 workload `echo`、`channel: "preview"` 与
`excludeComponentIds: ["cyrene-evaluation-exact-match"]`；stage 和 apply 重复相同 channel。匹配的 native release
和签名 Catalog binding 提供前，此路径为 **PENDING_RELEASE**，不得报告为已安装或已验收。

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

Until all exact release pins are populated, a fresh Ubuntu 24.04 host completes
the full clean-host acceptance, and the resulting receipts/services/API reads
are independently checked, report these gates as **NOT_RUN** or
**PENDING_RELEASE**. A native DEB installed, a workload staged, or a localhost
health response alone is partial evidence—not full modular-distribution
acceptance.

The previous GitHub-hosted acceptance attempt correctly failed its trusted-prefix
guard before installation because `/usr/share` and `/opt` were root-owned but
group/world writable (`0777`). Do not change those runner paths with `chmod` and
then describe the modified runner as pristine. Final acceptance is being moved
to a fresh official Ubuntu 24.04.5 guest with the standard root-owned `0755`
prefixes; the attempt remains **NOT_RUN** until its release and runtime evidence
is complete. This changes the acceptance host only, not the product deployment.

在填入全部精确 release pins、全新 Ubuntu 24.04 主机完成 clean-host acceptance，并独立核对 receipts、服务和
API readback 之前，各门禁均应标为 **NOT_RUN** 或 **PENDING_RELEASE**。仅安装 native DEB、暂存 workload 或
收到 localhost health 响应属于部分证据，不代表模块化发行整体验收完成。

先前 GitHub 托管验收在安装前被 trusted-prefix guard 正确拒绝：`/usr/share` 和 `/opt` 虽由 root 所有，但允许组/其他用户写入
（`0777`）。不要用 `chmod` 修改 runner 后再把它描述为 pristine。最终验收改在全新的官方 Ubuntu 24.04.5 guest 上运行，预期
前缀目录保持标准 root-owned `0755`；在完整取得发行物和运行时证据前，验收仍为 **NOT_RUN**。这只更换验收主机，不改变产品部署。
