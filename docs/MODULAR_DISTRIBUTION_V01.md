# Modular Distribution v0.1: Ubuntu installation / Ubuntu 安装指南

This preview targets Ubuntu 24.04 x86_64. Stage94’s exact Workspace source has an immutable Native release with official API/asset, exact-source, and strict-attestation readbacks passing. Stage93 guest2253 recorded three distinct Apply failures: `PLAN_CHANGED` and `PACKAGE_RUNTIME_READBACK_REQUIRED` with causes not established, then `FIRST_CORE_READBACK_REQUIRED` caused by the C10 authority probe defaulting to `cyrene-yield` instead of the signed Catalyst source. A later recovery Apply reported overall success with the SDK and six pointers healthy; the earlier failures remain recorded. Stage94 same-guest upgrade and new-source public Apply are pending, so core acceptance is not claimed.

此预览版面向 Ubuntu 24.04 x86_64。Stage94 的精确 Workspace 源码已发布为不可变 Native release，官方 API/资产、精确源码和严格 attestation 回读均已通过。Stage93 的 guest2253 记录了三次不同的 Apply 失败：`PLAN_CHANGED` 和 `PACKAGE_RUNTIME_READBACK_REQUIRED` 的原因未能确认；随后 `FIRST_CORE_READBACK_REQUIRED` 的原因是 C10 authority probe 默认选择 `cyrene-yield`，而不是签名 Catalyst source。之后一次 recovery Apply 报告 overall success，SDK 与六个 pointers 健康；此前失败仍作为独立回执保留。Stage94 同一主机的 upgrade 和新源码 public Apply 尚待执行，因此不声称 core 验收通过。

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

<a id="stage94-native-install"></a>

## Current Native download and Catalyst install / 当前 Native 下载与 Catalyst 安装

The current immutable Native release is [native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968), built from Workspace develop `03453ed1c053151c9af86a2c0a185747b55c3968` (PR #166). Release API ID `410782500` was produced by run `38272522755`. All 18 downloaded assets match the immutable release API SHA-256 digests and byte sizes. The exact-source verifier passed 19 attestation invocations (nine direct Native subjects and ten nested Product subjects), with no skip or trust cache. The Ubuntu 24.04 DEB is `cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb`, SHA-256 `12987b876a6918608efa6973a9029d0840517d6e4a52e5252cb4ef1f0e4271b6`, size `155115228` bytes. Platform P `587d35588e8590abab5bd645ba09bbc480479daa`, Plugins G `c9b8d4b7d4923d42368de3b3ec2db87ee97c3fa7` (SDK `0.2.1`), signed Catalog generation 15, and selected Product identities are unchanged from Stage93. PR #166 fixes C10 authority-source selection using the validated signed plan source while retaining authority checks. These release checks do not establish Stage94 workload acceptance; the same-guest upgrade and public Apply remain pending.

当前不可变 Native release 为 [native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968)，基于 Workspace develop `03453ed1c053151c9af86a2c0a185747b55c3968`（PR #166）构建。Release API ID 为 `410782500`，producer run 为 `38272522755`。全部 18 项下载资产均与不可变 release API 的 SHA-256 和字节大小匹配。精确源码 verifier 的 19 次 attestation 调用（9 个 Native 直接 subject、10 个嵌入 Product subject）全部通过，未跳过或使用信任缓存。Ubuntu 24.04 DEB 为 `cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb`，SHA-256 为 `12987b876a6918608efa6973a9029d0840517d6e4a52e5252cb4ef1f0e4271b6`，大小为 `155115228` 字节。Platform P `587d35588e8590abab5bd645ba09bbc480479daa`、带 SDK `0.2.1` 的 Plugins G `c9b8d4b7d4923d42368de3b3ec2db87ee97c3fa7`、已签名 Catalog generation 15 和所选 Product 身份均与 Stage93 相同。PR #166 修复了 C10 authority source 选择，使用已验证签名 plan 中的 source，同时保留 authority 校验。这些发行回读不代表 Stage94 workload 验收通过；同一主机升级和 public Apply 仍待执行。

The separate Catalyst/Echo cloud update check is not verified: its pinned catalog helper rejects the `deploymentRoles` property in `compatibilityGroups.0` before Azure deployment. This schema mismatch does not affect the Ubuntu Native release or its local acceptance; the cloud app update remains unverified.

单独的 Catalyst/Echo 云端更新检查尚未验证：固定版本 catalog helper 在 Azure 部署前拒绝了 `compatibilityGroups.0` 中的 `deploymentRoles` 属性。该 schema 不匹配不影响 Ubuntu Native release 或其本机验收；云端 app 更新仍未验证。

### Verify the GitHub CLI prerequisite / 校验 GitHub CLI 前置工具

The Native DEB's detached attestation is verified with GitHub CLI. A clean Ubuntu 24.04 host does not include this tool, so install the official `cli/cli` v2.102.0 package before running `gh attestation verify`. The acceptance workflow pins release commit `fc4b137cdef0a6bd28fd461b7cf9c84a5812a8cd`, the amd64 DEB SHA-256/size, and the checksum-list SHA-256/size below. This step checks the official release API identity and asset metadata, downloaded bytes, DEB package fields, and the installed version. It does not skip or replace the Native DEB attestation check.

Native DEB 的 detached attestation 使用 GitHub CLI 验证。全新 Ubuntu 24.04 主机默认没有该工具，因此先安装官方 `cli/cli` v2.102.0，再运行 `gh attestation verify`。验收 workflow 固定了 release commit `fc4b137cdef0a6bd28fd461b7cf9c84a5812a8cd`、amd64 DEB 的 SHA-256/大小，以及 checksum 文件的 SHA-256/大小。以下步骤会核对官方 release API 身份与资产元数据、下载字节、DEB 包字段和安装后的版本；它不会跳过或替代 Native DEB 的 attestation 验证。

```bash
set -euo pipefail
GH_CLI_VERSION='2.102.0'
GH_CLI_TAG="v$GH_CLI_VERSION"
GH_CLI_SOURCE_COMMIT='fc4b137cdef0a6bd28fd461b7cf9c84a5812a8cd'
GH_CLI_DEB='gh_2.102.0_linux_amd64.deb'
GH_CLI_DEB_SHA256='7e54a307f90afdc59796c325ec0c49fb09e6c18537727207a8ac7513584ea5b0'
GH_CLI_DEB_SIZE_BYTES='15392446'
GH_CLI_CHECKSUMS='gh_2.102.0_checksums.txt'
GH_CLI_CHECKSUMS_SHA256='afe49e9affa232faa8212aed035417166f6ade9b9470acb53d4dbd28c0504e8d'
GH_CLI_CHECKSUMS_SIZE_BYTES='1971'
GH_CLI_RELEASE_URL="https://github.com/cli/cli/releases/download/$GH_CLI_TAG"
GH_CLI_RELEASE_API='https://api.github.com/repos/cli/cli/releases/tags/v2.102.0'
GH_CLI_DIR="$(mktemp -d)"
curl --fail --location --silent --show-error \
  --header 'Accept: application/vnd.github+json' \
  --header 'X-GitHub-Api-Version: 2022-11-28' \
  --output "$GH_CLI_DIR/release-api.json" "$GH_CLI_RELEASE_API"
python3 - "$GH_CLI_DIR/release-api.json" \
  "$GH_CLI_TAG" "$GH_CLI_SOURCE_COMMIT" \
  "$GH_CLI_DEB" "$GH_CLI_DEB_SHA256" "$GH_CLI_DEB_SIZE_BYTES" "$GH_CLI_RELEASE_URL/$GH_CLI_DEB" \
  "$GH_CLI_CHECKSUMS" "$GH_CLI_CHECKSUMS_SHA256" "$GH_CLI_CHECKSUMS_SIZE_BYTES" "$GH_CLI_RELEASE_URL/$GH_CLI_CHECKSUMS" <<'PY'
import json
import sys
from pathlib import Path

def require(condition, message):
    if not condition:
        raise SystemExit(message)

(
    api_path, expected_tag, expected_source,
    deb_name, deb_sha256, deb_size, deb_url,
    sums_name, sums_sha256, sums_size, sums_url,
) = sys.argv[1:]
release = json.loads(Path(api_path).read_text(encoding="utf-8"))
require(release.get("tag_name") == expected_tag, "official GitHub CLI release tag differs from the pin")
require(release.get("target_commitish") == expected_source, "official GitHub CLI source commit differs from the pin")
require(release.get("immutable") is True and release.get("draft") is False, "official GitHub CLI release is not immutable and published")
asset_rows = release.get("assets")
require(isinstance(asset_rows, list), "official GitHub CLI release has no asset list")
assets = {row.get("name"): row for row in asset_rows if isinstance(row, dict)}
require(len(assets) == len(asset_rows), "official GitHub CLI release has malformed or duplicate assets")
for name, digest, size, url in (
    (deb_name, deb_sha256, deb_size, deb_url),
    (sums_name, sums_sha256, sums_size, sums_url),
):
    asset = assets.get(name)
    require(isinstance(asset, dict), f"official GitHub CLI release omits {name}")
    require(asset.get("digest") == f"sha256:{digest}", f"GitHub API digest for {name} differs from the pin")
    require(asset.get("size") == int(size), f"GitHub API size for {name} differs from the pin")
    require(asset.get("browser_download_url") == url, f"canonical download URL for {name} differs from the pin")
PY
curl --fail --location --silent --show-error \
  --output "$GH_CLI_DIR/$GH_CLI_DEB" "$GH_CLI_RELEASE_URL/$GH_CLI_DEB"
curl --fail --location --silent --show-error \
  --output "$GH_CLI_DIR/$GH_CLI_CHECKSUMS" "$GH_CLI_RELEASE_URL/$GH_CLI_CHECKSUMS"
test "$(stat -c '%s' "$GH_CLI_DIR/$GH_CLI_DEB")" = "$GH_CLI_DEB_SIZE_BYTES"
test "$(stat -c '%s' "$GH_CLI_DIR/$GH_CLI_CHECKSUMS")" = "$GH_CLI_CHECKSUMS_SIZE_BYTES"
printf '%s  %s\n' "$GH_CLI_DEB_SHA256" "$GH_CLI_DEB" \
  | (cd "$GH_CLI_DIR" && sha256sum --check --strict)
printf '%s  %s\n' "$GH_CLI_CHECKSUMS_SHA256" "$GH_CLI_CHECKSUMS" \
  | (cd "$GH_CLI_DIR" && sha256sum --check --strict)
test "$(grep -Fxc -- "$GH_CLI_DEB_SHA256  $GH_CLI_DEB" "$GH_CLI_DIR/$GH_CLI_CHECKSUMS")" = '1'
test "$(dpkg-deb -f "$GH_CLI_DIR/$GH_CLI_DEB" Package Version Architecture)" = "$(printf '%s\n' 'Package: gh' 'Version: 2.102.0' 'Architecture: amd64')"
sudo apt-get install -y --no-install-recommends "$GH_CLI_DIR/$GH_CLI_DEB"
GH_INSTALLED_VERSION="$(gh --version | sed -n '1s/^gh version \([0-9][0-9.]*\).*/\1/p')"
test "$GH_INSTALLED_VERSION" = "$GH_CLI_VERSION"
printf 'GitHub CLI %s is installed and verified.\n' "$GH_INSTALLED_VERSION"
```

Only after this version readback succeeds, download and independently verify the Native DEB attestation below. Do not add a `--skip` path if the CLI or attestation check fails.

只有确认安装后版本读回成功，才继续下载并独立验证下方 Native DEB attestation。若 CLI 或 attestation 检查失败，不得添加 `--skip` 路径。

```bash
set -euo pipefail
WORKSPACE_REPOSITORY='DoHorizon-AI/Cyrene-Workspace'
NATIVE_TAG='native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968'
NATIVE_SOURCE_SHA='03453ed1c053151c9af86a2c0a185747b55c3968'
NATIVE_DEB='cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb'
NATIVE_DEB_SHA256='12987b876a6918608efa6973a9029d0840517d6e4a52e5252cb4ef1f0e4271b6'
NATIVE_DEB_SIZE_BYTES='155115228'
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
sudo cyrene workload install catalyst --channel preview
```

Run the Catalyst install interactively so you can record the displayed `planId` and `planDigest` before confirming. The `preview` channel resolves the trusted candidate available when the plan is created, and that plan pins exact immutable release identities and digests. Record the actual source tuple from the returned plan and installed-state readbacks. `--yes` suppresses the confirmation prompt and omits plan identity when an operation fails; `workload status` does not list pending plans. If a retryable Apply failure requires recovery, do not rerun guided install because it creates a new check/stage rather than resuming the exact plan. The supported public resume is `sudo cyrene workload --json` with `operation:"apply"` and the original plan fields below; there is no `workload apply --plan-id` subcommand.

请使用交互式 Catalyst 安装，以便在确认前记录显示的 `planId` 和 `planDigest`。`preview` channel 会解析创建 plan 时可用的受信候选版本，plan 随后固定精确的不可变 release 身份和 digest。应从返回的 plan 与 installed-state 回读记录实际来源 tuple。`--yes` 会跳过确认提示，操作失败时不会显示 plan identity；`workload status` 不会列出待处理 plan。若 Apply 可重试失败后需要恢复，不要重新运行 guided install，因为它会创建新的 check/stage，而不是续用原 plan。公开支持的恢复入口是 `sudo cyrene workload --json`，请求使用 `operation:"apply"` 和下方原 plan 字段；没有 `workload apply --plan-id` 子命令。

Replace every placeholder below with the exact values saved from the original plan. Keep the original `planId`, `planDigest`, and channel unchanged:

以下命令中的占位符必须替换为原 plan 保存的精确值。保持原 `planId`、`planDigest` 和 channel 不变：

```bash
printf '%s\n' \
  '{"protocolVersion":"cyrene.workload-plan.v1","operation":"apply","workloadId":"catalyst","targetId":"linux-ubuntu-24.04-x86_64","planId":"<original-plan-id>","planDigest":"sha256:<original-64-hex-digest>","confirmation":{"planId":"<original-plan-id>","planDigest":"sha256:<original-64-hex-digest>","confirmed":true},"action":"install","channel":"<original-stable-or-preview-channel>"}' \
  | sudo cyrene workload --json | jq .
```


Keep the original plan identity, command output, and transaction receipts if Apply must be resumed. The runtime acceptance associated with this release is still in progress; see the concise status below.

若需要恢复 Apply，请保存原 plan 身份、命令输出和事务回执。本 release 对应的 runtime 验收仍在进行；当前状态见下方简表。

<a id="standalone-plugin"></a>

## Install one Plugin / 单独安装 Plugin

Use the `plugins` workload with one explicit component selection. Example check for Document Parsing:

通过 `plugins` workload 显式选择一个组件。以下示例检查 Document Parsing：

    sudo cyrene workload --json <<'JSON' | jq .
    {"protocolVersion":"cyrene.workload-plan.v1","operation":"check","workloadId":"plugins","channel":"preview","targetId":"linux-ubuntu-24.04-x86_64","action":"install","selections":{"includeComponentIds":["cyrene-tools-document-parsing"],"excludeComponentIds":[],"choices":{}}}
    JSON

Use the returned `planId` and `planDigest` unchanged for stage and apply. Empty selection is blocked; it does not install every Plugin.

stage 与 apply 使用返回的 `planId` 和 `planDigest`，保持其值不变。空选择会被拒绝，不会安装全部 Plugin。

<a id="echo"></a>

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

Keep Client and Control bound to `127.0.0.1`. The expected local Client address is `http://127.0.0.1:8100/`; Stage94 browser acceptance is pending its actual receipt. For remote access, use an approved SSH local forward and keep the service bound to loopback:

Client 与 Control 仅绑定到 `127.0.0.1`。预期本机 Client 地址为 `http://127.0.0.1:8100/`；Stage94 浏览器验收仍等待实际回执。远程访问使用获准的 SSH 本地转发，服务仍保持 loopback 绑定：

    ssh -L 8100:127.0.0.1:8100 user@ubuntu-host

This preview supports Ubuntu 24.04 x86_64 only. Release signatures and successful package installation do not prove Plugin invocation, data processing, SFT loading, Client use, restart recovery, or Echo evaluation. Tie each result to exact release identities; mark unrun checks `NOT_RUN`.

此预览版仅支持 Ubuntu 24.04 x86_64。发行签名与包安装成功不能证明 Plugin 调用、数据处理、SFT 加载、Client 使用、重启恢复或 Echo 评测通过。每项结果都应绑定精确发行身份；未运行的检查标记为 `NOT_RUN`。

The preview has no customer-facing arbitrary-version downgrade command. On failure, preserve receipts and the transaction journal and use the supported maintenance path. Uninstall preserves user datasets, artifacts, review history, and backups by default; keep separate backups.

预览版没有面向客户的任意版本降级命令。发生故障时保留 receipt 与 transaction journal，并使用受支持的 maintenance 流程。默认卸载会保留用户 Dataset、Artifact、审核历史和备份；仍需单独保留备份。

See the [architecture decision record](ADR_MODULAR_DISTRIBUTION_V01.md) for system boundaries and release evidence.

系统边界与发行证据见[架构决策记录](ADR_MODULAR_DISTRIBUTION_V01.md)。

## Release identity and acceptance status / 发行身份与验收状态

The current release is an immutable, source-bound Native preview. The signed plan pins exact package identities when created; selecting `preview` later may resolve a newer candidate, so record the plan and installed-state source tuple for each installation.

当前 release 是不可变且绑定源码的 Native preview。签名 plan 会在创建时固定精确 package 身份；之后选择 `preview` 可能解析到更新的候选版本，因此每次安装都应记录 plan 和 installed-state 返回的来源 tuple。

| Component | Source / identity | Current status |
| --- | --- | --- |
| Workspace Native | `03453ed1c053151c9af86a2c0a185747b55c3968` (PR #166) | [Immutable release](https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/native-installer-preview-03453ed1c053151c9af86a2c0a185747b55c3968); API `410782500`; producer run `38272522755`; 18 assets matched; 9 direct + 10 nested subjects; 19 strict attestations |
| Ubuntu 24.04 amd64 DEB | Asset `cyrene_0.1.0-rc.1_ubuntu-24.04_amd64.deb` | SHA-256 `12987b876a6918608efa6973a9029d0840517d6e4a52e5252cb4ef1f0e4271b6`; `155115228` bytes |
| Platform | `587d35588e8590abab5bd645ba09bbc480479daa` (PR #109) | Existing immutable release reused; 52 assets verified |
| Plugins Official | `c9b8d4b7d4923d42368de3b3ec2db87ee97c3fa7` (PR #119) | Five data Plugins and SDK carrier verified; SDK `0.2.1` |
| Signed v2 Catalog | source `18455255cdf8b87dd8cd48ceb25e5ea411a622ad`; generation 15; SHA-256 `9356861a377e18d1b6ed586d7ee8316de7d7d11c5e7543269d3cbd5d456f314d` | Unchanged |
| Catalyst / Echo / Client | `4ad95061bde8c5d216a8cb7b30e1ef2ae1b4d49b` / `d5a920078077bdbaa76e7117ad83f69cbfa1c614` / `b3c3f964540e3aa761612371891e3bb6a3f7ad59` | Existing Product release identities reused |
| Data Plugins | Preparation `0.2.1`; Parsing `0.2.1`; Generation `0.2.2`; Knowledge Preparation `0.1.1`; Exact Match `0.1.1` | Immutable signed releases reused |

Stage94 publisher and exact-source verifier receipts confirm the immutable release identity, all 18 API asset digests and byte sizes, eleven exact source files, and 19 strict attestations. These checks prove the published release and provenance; they do not prove successful workload execution.

Stage94 publisher 与精确源码 verifier 回执确认了不可变发行身份、18 项资产的 API digest 和字节大小、11 个精确源码文件及 19 次严格 attestation。这些检查证明发行物和来源，不证明 workload 已成功运行。

| Acceptance gate | Status as of this update |
| --- | --- |
| Stage94 Native API/assets, exact source, and strict attestations | PASS: 18/18 assets match API digests/sizes; eleven-file source readback; 19/19 invocations (9 direct + 10 nested), no skip or trust cache |
| Stage93 guest2253 Apply #1 | FAILED: `PLAN_CHANGED`; cause not established |
| Stage93 guest2253 same-plan Apply #2 | FAILED: `PACKAGE_RUNTIME_READBACK_REQUIRED`; cause not established and not attributed to the later C10 bug |
| Stage93 guest2253 Apply #3 | FAILED: `FIRST_CORE_READBACK_REQUIRED`; C10 probe defaulted to `cyrene-yield` because the validated signed `activitySources` source ID was omitted; Catalyst was selected |
| Stage93 later recovery Apply | LIMITED PASS: Root reports overall Apply success with SDK and six pointers healthy; earlier failure receipts remain failures and this is not Stage94 core acceptance |
| PR #166 / Stage94 C10 source fix | MERGED: select the C10 authority source from the validated signed plan while retaining authority checks |
| Stage94 same-guest stage-only upgrade and new-source public Apply | PENDING: no Stage94 host acceptance receipt has been recorded |
| Stage92 Catalyst Apply on guest2252 | FAILED: required signed Client Web `static-web` was rejected by a blanket core-reuse veto; refs-only restart hit a transient active-state failure and rolled back |
| Stage92 root cause / PR #165 source fix | MERGED: removed only the `web_selected` veto; retained OCI exclusion and signed live-proof checks |
| Stage94 full core acceptance | PENDING: same-guest upgrade, new-source public Apply, and required downstream gates remain open |
| Catalyst data workflow and SFT package/independent consumer | NOT_RUN: Stage94 host acceptance is pending |
| Client/browser | NOT_RUN: no current browser result recorded |
| Echo install/evaluation/removal | NOT_RUN: no current result recorded |
| Full core recovery, lifecycle retention, and reboot | PENDING: the later Stage93 recovery Apply was limited to the reported SDK/six-pointer result; Stage94 proof is still required |
| Catalyst/Echo cloud update check | NOT_VERIFIED: pinned helper/schema rejected `deploymentRoles` before Azure deployment; Ubuntu Native is unaffected |
| Historical Stage91 Catalyst Apply | FAILED (retryable): `PACKAGE_RUNTIME_READBACK_REQUIRED`; this is not the Stage94 result |

| 验收门禁 | 本次更新时的状态 |
| --- | --- |
| Stage94 Native API/资产、精确源码与严格 attestation | 通过：18/18 项资产匹配 API digest/大小；11 个源码文件精确回读；19/19 次调用（9 直接 + 10 嵌套）；无跳过或信任缓存 |
| Stage93 guest2253 Apply #1 | 失败：`PLAN_CHANGED`；原因未能确认 |
| Stage93 guest2253 同一 plan Apply #2 | 失败：`PACKAGE_RUNTIME_READBACK_REQUIRED`；原因未能确认，不能归因于后续 C10 bug |
| Stage93 guest2253 Apply #3 | 失败：`FIRST_CORE_READBACK_REQUIRED`；C10 probe 未取得已验证签名 `activitySources` source ID，默认选择 `cyrene-yield`；plan 实际选择 Catalyst |
| Stage93 后续 recovery Apply | 有限通过：Root 报告整体 Apply success，SDK 与六个 pointers 健康；此前失败回执仍保留为失败，且不代表 Stage94 core 验收通过 |
| PR #166 / Stage94 C10 源码修复 | 已合并：从已验证签名 plan 选择 C10 authority source，同时保留 authority 校验 |
| Stage94 同一主机 stage-only upgrade 与新源码 public Apply | 等待中：尚无 Stage94 主机验收回执 |
| guest2252 Stage92 Catalyst Apply | 失败：helper 一票否决签名 Catalog 中必需的 Client Web `static-web`，refs-only restart 遇到暂态 active-state 错误后回滚 |
| Stage92 根因与 PR #165 源码修复 | 已合并：仅移除 `web_selected` 一票否决，保留 OCI 排除和签名 live-proof 校验 |
| Stage94 完整 core 验收 | 等待中：同一主机 upgrade、新源码 public Apply 和后续门禁尚未完成 |
| Catalyst 数据流程、SFT 包及独立 consumer | 未运行：Stage94 主机验收仍待执行 |
| Client/浏览器 | 未运行：无当前浏览器结果 |
| Echo 安装/评测/移除 | 未运行：无当前结果 |
| 完整 core recovery、生命周期保留与重启 | 等待中：Stage93 后续 recovery Apply 仅有 SDK/六个 pointers 健康回读；仍需 Stage94 验收 |
| Catalyst/Echo 云端更新检查 | 未验证：固定 helper/schema 在 Azure 部署前拒绝 `deploymentRoles`；不影响 Ubuntu Native |
| Stage91 历史 Catalyst Apply | 失败（可重试）：`PACKAGE_RUNTIME_READBACK_REQUIRED`；此结果不是 Stage94 验收结果 |

Earlier preview attempts exposed and fixed package/runtime identity and recovery issues through normal Workspace/Platform changes. Those attempts are historical diagnostics; they are not results for this release. The initial cause of the superseded Stage90 Apply failure was not established. The older guest2246 PackageOnly hold is not claimed as recovered. Do not infer current acceptance from CI, release signatures, or a successful DEB install.

此前 preview 尝试通过正常的 Workspace/Platform 修复处理了 package/runtime 身份与恢复问题。这些尝试属于历史诊断，不是当前 release 的验收结果。已被替代的 Stage90 Apply 失败，其最初原因未能确定。这里不声称旧 guest2246 PackageOnly hold 已恢复。不能从 CI、release 签名或 DEB 安装成功推断当前验收通过。
