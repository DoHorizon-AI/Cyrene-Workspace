# Modular Distribution v0.1: Ubuntu installation / Ubuntu 安装指南

**Status:** operator instructions for the modular-distribution candidate. Do not
install until the exact immutable releases below are published and verified.
At the time this guide was prepared, the matching generation-15 native DEB,
Client Control release, and official Linux Echo release are not available. The
new clean-host release acceptance is therefore **NOT_RUN**. A successful local
check, a staged DEB, or an older release does not change that status.

**状态：** 本文说明模块化发行候选版的操作步骤。只有下述精确不可变发行物正式发布并验签后才能安装。
编写本文时，匹配 generation 15 的 native DEB、Client Control 和 Echo 官方 Linux 发行物尚未发布；
全新主机发行验收为 **NOT_RUN**。本地检查通过、DEB 已暂存或旧版发行物均不能改变此状态。

The Client static Web archive is already published as
`preview-cyrene-client-workspace-web-297357f08377aee9819d5f14707ec1f8fac3aa88`.
It is a static payload, not a server or proof of full Workspace installation.
Workspace serves the activated `current` tree through its managed loopback Nginx
host at `http://127.0.0.1:8100/`; the host proxies same-origin Control requests
to `127.0.0.1:5182`. Do not expose either listener on a public interface. Use an
existing approved SSH local forward when accessing the host remotely; installation
preserves existing SSH-forwarding configuration by default.

Client 静态 Web 包已发布为
`preview-cyrene-client-workspace-web-297357f08377aee9819d5f14707ec1f8fac3aa88`，但它只是静态内容，
不是 HTTP 服务，也不能证明 Workspace 安装已完成。Workspace 通过受管的 loopback Nginx 服务
`http://127.0.0.1:8100/`，并将同源 Control 请求转发至 `127.0.0.1:5182`。不要把任一 listener 暴露到公网；
远程访问使用现有且获准的 SSH 本地转发。安装默认保留已有 SSH 转发配置。

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
immutable release assets directly; no repository clone or source build is needed.
Verify the release checksum list, then verify the DEB and v2 release manifest
against the exact repository, publisher workflow, source ref, and source SHA.
Verify the v2 source receipt's active-v2 catalog release ID and source identity,
then verify its exact catalog bytes, generation, and detached attestation. Stop
if any identity differs or if the release lacks the v2 manifest, source receipt,
DEB/catalog attestation bundles, or expected generation-15 catalog binding.

占位符未替换前不要运行以下命令。可直接下载不可变 release assets，无需 clone 仓库或构建源码。
先校验 release checksum，再按确切仓库、发布 workflow、source ref 和 source SHA 验证 DEB 与 v2
release manifest。再校验 v2 source receipt 中 active-v2 Catalog 的 release ID/source identity，并验证其确切
Catalog bytes、generation 与 detached attestation。任一身份不符，或缺少 v2 manifest、source receipt、DEB/Catalog
attestation bundle 或预期的 generation-15 Catalog binding，都应停止安装。

```bash
set -euo pipefail
mkdir -p "$RELEASE_DIR"
gh release download "$NATIVE_RELEASE_ID" \
  --repo "$WORKSPACE_REPOSITORY" \
  --dir "$RELEASE_DIR"

cd "$RELEASE_DIR"
sha256sum --check --strict SHA256SUMS
test -s "$NATIVE_DEB_ASSET"
test -s "$NATIVE_MANIFEST_ASSET"
test -s "$NATIVE_SOURCE_RECEIPT_ASSET"
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
stage, digest validation, or the trusted updater. The default Catalyst selection
contains the required Catalyst service, Client Control, Client Web, and Runtime
Maintenance SDK, plus four recommended data plugins:

向导命令检查 Catalog、显示解析出的组件身份、暂存精确计划，并且只按相同 plan ID 与 digest 执行 apply。
`--yes` 只确认本次命令生成的计划，不跳过 check、stage、digest 校验或可信 updater。Catalyst 默认选择包含
必需的 Catalyst 服务、Client Control、Client Web、Runtime Maintenance SDK，以及四个推荐数据插件：

- `cyrene-tools-dataset-preparation`
- `cyrene-tools-document-parsing`
- `cyrene-tools-dataset-generation`
- `cyrene-tools-knowledge-preparation`

```bash
sudo cyrene workload install catalyst --yes
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"status","workloadId":"catalyst"}
JSON
```

Required components cannot be excluded. If a required component is excluded,
`check` returns a blocker and no plan may proceed to stage or apply. Recommended
components are selected by default and may be explicitly excluded in `check`.
For example, to omit only Document Parsing, check with:

必需组件不能取消；若排除必需组件，`check` 会返回 blocker，计划不得进入 stage 或 apply。推荐组件默认纳入，
可在 `check` 中显式排除。以下示例只取消 Document Parsing：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"check","workloadId":"catalyst","targetId":"linux-ubuntu-24.04-x86_64","action":"install","selections":{"includeComponentIds":[],"excludeComponentIds":["cyrene-tools-document-parsing"],"choices":{}}}
JSON
```

Proceed only when the returned `result.status` is `ready` and the selected rows
match the intended choices. Copy that response's exact `planId` and `planDigest`
into the stage request:

只有在返回的 `result.status` 为 `ready` 且所选组件符合预期时才继续。将该响应中的精确 `planId` 与
`planDigest` 填入 stage 请求：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"stage","workloadId":"catalyst","targetId":"linux-ubuntu-24.04-x86_64","action":"install","planId":"<planId-from-check>","planDigest":"sha256:<64-lowercase-hex-from-check>"}
JSON
```

Review the staged rows and only then apply the same action, plan ID, and digest:

检查 stage 返回的组件行；确认后才用相同 action、plan ID 和 digest 执行 apply：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"apply","workloadId":"catalyst","targetId":"linux-ubuntu-24.04-x86_64","action":"install","planId":"<same-planId>","planDigest":"sha256:<same-64-hex>","confirmation":{"planId":"<same-planId>","planDigest":"sha256:<same-64-hex>","confirmed":true}}
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

Example `check` for Document Parsing; if ready, use its exact `planId` and
`planDigest` in the stage and apply requests shown above, changing `workloadId`
to `plugins` and preserving the same selection in the check:

以下以 Document Parsing 为例执行 `check`；若状态为 `ready`，在上文 stage/apply 请求中将
`workloadId` 改为 `plugins`，并使用 check 返回的精确 `planId` 和 `planDigest`：

```bash
sudo cyrene workload --json <<'JSON' | jq .
{"protocolVersion":"cyrene.workload-plan.v1","operation":"check","workloadId":"plugins","targetId":"linux-ubuntu-24.04-x86_64","action":"install","selections":{"includeComponentIds":["cyrene-tools-document-parsing"],"excludeComponentIds":[],"choices":{}}}
JSON
```

The `plugins` workload requires at least one explicit optional selection. An
empty selection is blocked; it does not install every plugin. Each selected
package must resolve through the signed Catalog and exact publisher release
index/manifest/attestation chain.

`plugins` workload 至少要求一个显式可选选择项。空选择会被 blocker 拒绝，不会安装全部插件。每个选择的包都必须
通过签名 Catalog 和确切 publisher release index/manifest/attestation 链解析。

## 4. Add Echo / 增装 Echo

Echo is a separate workload. Once its official Linux OCI release and the matching
Client Control/native DEB releases are published and pinned, this command adds
Echo and its default recommended exact-match evaluator without replacing or
uninstalling Catalyst:

Echo 是独立 workload。只有在 Echo 官方 Linux OCI 与匹配的 Client Control/native DEB 发行物发布并完成 pin 后，
此命令才会增装 Echo 及默认推荐的 exact-match evaluator；它不会替换或卸载 Catalyst：

```bash
sudo cyrene workload install echo --yes
```

To omit the evaluator, use the same check/stage/apply protocol with workload
`echo` and `excludeComponentIds: ["cyrene-evaluation-exact-match"]`. Until the
official Echo release is available, this path is **PENDING_RELEASE** and must
not be reported as installed or accepted.

若不安装 evaluator，使用相同的 check/stage/apply 协议并设置 workload `echo` 与
`excludeComponentIds: ["cyrene-evaluation-exact-match"]`。Echo 官方发行物发布前，此路径为
**PENDING_RELEASE**，不得报告为已安装或已验收。

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

在填入全部精确 release pins、全新 Ubuntu 24.04 主机完成 clean-host acceptance，并独立核对 receipts、服务和
API readback 之前，各门禁均应标为 **NOT_RUN** 或 **PENDING_RELEASE**。仅安装 native DEB、暂存 workload 或
收到 localhost health 响应属于部分证据，不代表模块化发行整体验收完成。
