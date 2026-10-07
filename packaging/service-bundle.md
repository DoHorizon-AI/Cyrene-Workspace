# Linux service bundle inputs / Linux 服务发布包输入

The Debian package includes five offline, per-service Python releases:
Navigator, Yield, Reactor, Exchange, and Catalyst. `build-deb.sh` still creates
that complete installation package. The public component producer can build
one service independently for a component release; it does not require a new
five-service DEB for each Product change. Echo is supported as an independent
Linux Python bundle for the data-tools trial; it is not added to the existing
five-service Debian installation or systemd cohort.

DEB 包离线携带 Navigator、Yield、Reactor、Exchange、Catalyst 五个独立 Python
服务版本。任何服务缺少 wheelhouse，或来源 SHA 与 `release-lock.json` 不匹配时，
`build-deb.sh` 都会明确失败，不会生成只有 unit、没有服务实现的包。

Echo 也可单独生成供 Catalyst/Echo 数据工具试用使用的 Linux Python bundle；该 bundle 不属于上述
DEB/systemd 五服务集合。准备工作目录和 bundle 的命令见本页英文 “Preparing inputs” 部分。

## Wheelhouse layout / Wheelhouse 目录

Pass the root directory with `--service-wheelhouse <dir>` or
`CYRENE_SERVICE_WHEELHOUSE=<dir>`. It must contain these flat directories:

```text
<wheelhouse>/
├── navigator/
│   ├── requirements.lock
│   ├── source.json
│   ├── serve-web.py
│   └── *.whl
├── yield/       # requirements.lock, source.json, and *.whl
├── reactor/     # requirements.lock, source.json, and *.whl
├── exchange/    # requirements.lock, source.json, and *.whl
└── catalyst/    # requirements.lock, source.json, and *.whl
```

`requirements.lock` is a pip requirements file with exact `name==version`
pins and SHA-256 hashes for every transitive dependency and application wheel.
It must not contain VCS/URL requirements or index directives. Every referenced
wheel must be present in the same service directory. The bundle builder installs
with `pip --no-index --no-deps --require-hashes --only-binary=:all:`; every
runtime dependency is therefore an explicit, hashed lock entry and pip cannot
follow a wheel's direct Git metadata to the network. An incomplete or
inconsistent wheel set stops the package build.

New Product wheelhouses use `source.json` schema v2. The service and lock must
match the selected immutable source revision, and the SDK fields must match the
verified `cyrene-runtime-maintenance-sdk` release inputs:

```json
{
  "schema_version": 2,
  "service": "navigator",
  "source_repository": "Cyrene-Navigator",
  "source_commit": "<40-character SHA from release-lock.json>",
  "requirements_lock_sha256": "<SHA-256 of requirements.lock>",
  "runtime_dependencies": [
    {
      "component_id": "cyrene-runtime-maintenance-sdk",
      "manifest_digest": "sha256:<64 lowercase hex>",
      "artifact_digest": "sha256:<64 lowercase hex>",
      "distribution": "cyrene-runtime-maintenance",
      "version": "0.1.0",
      "wheel_sha256": "<64 lowercase hex>"
    }
  ]
}
```

The bundle's inner `manifest.json` is schema v2 and repeats SDK provenance
under `dependencies.runtime`. Its lock file pins the exact wheel bytes. The
validator retains schema v1 read support for already installed releases and
rollback; new releases can be staged only from v2 bundles.

The application distributions required in each lock are `cyrene-navigator`,
`cyrene-yield`, `cyrene-reactor-product`, `cyrene-exchange` plus
`cyrene-exchange-product`, `cyrene-catalyst`, and `cyrene-echo`. Navigator also
needs the `scripts/serve-web.py` launcher from its pinned source revision.

## Preparing inputs / 准备输入

Generate wheelhouses from immutable Product source revisions and an independently
verified Runtime Maintenance SDK wheel. Do not use whichever sibling checkout
happens to be current. The following prepares only Catalyst; omit `--service`
and `--service-commit` to prepare all five DEB services:

```bash
python3.12 packaging/prepare_service_wheelhouse.py \
  --service catalyst \
  --service-commit <40-character-product-source-SHA> \
  --output /path/to/service-wheelhouse \
  --runtime-sdk-wheel /path/to/verified/cyrene_runtime_maintenance-0.1.0-py3-none-any.whl \
  --runtime-sdk-version 0.1.0 \
  --runtime-sdk-sha256 <raw-64-character-wheel-SHA> \
  --runtime-sdk-manifest-digest sha256:<64-character-manifest-digest> \
  --runtime-sdk-artifact-digest sha256:<64-character-bundle-digest>
```

The preparation command needs Python 3.12 with pip, Git, uv, and network
access to the selected pinned source repository and configured Python package
index. It checks out the exact source SHA in an isolated temporary directory,
checks the service lock, builds application and locked Git/path dependency
wheels, exports a hash-pinned runtime requirements file, and downloads the
complete wheel set. It verifies the SDK wheel's exact distribution, version,
and SHA-256 and records its manifest and bundle digests. Navigator's wheelhouse
also includes `scripts/serve-web.py` from its pinned checkout. The package
builder rejects Navigator launchers that do not accept the secure
`--pairing-code-file` option or that print the pairing secret.

The producer runs the single-service bundle builder with the same service SHA:

```bash
python3.12 packaging/service_bundle.py build \
  --service catalyst \
  --service-commit <40-character-product-source-SHA> \
  --wheelhouse /path/to/service-wheelhouse \
  --release-lock release-lock.json \
  --output /path/to/service-bundles \
  --arch amd64
```

The builder outputs one immutable `manifest.json` plus the complete runtime
bundle. Release workflows validate that output and publish it without rewriting
its inner manifest.

Echo can be prepared independently from its exact `release-lock.json` source
revision using the same Runtime Maintenance SDK provenance inputs:

```bash
python3.12 packaging/prepare_service_wheelhouse.py \
  --service echo \
  --output /path/to/echo-wheelhouse \
  --target-profile linux-ubuntu-24.04-x86_64-python-3.12 \
  --python-executable /path/to/verified/cpython-3.12.14/bin/python3.12 \
  --uv /path/to/verified/uv \
  --runtime-sdk-wheel /path/to/verified/cyrene_runtime_maintenance-0.1.0-py3-none-any.whl \
  --runtime-sdk-version 0.1.0 \
  --runtime-sdk-sha256 <raw-64-character-wheel-SHA> \
  --runtime-sdk-manifest-digest sha256:<64-character-manifest-digest> \
  --runtime-sdk-artifact-digest sha256:<64-character-bundle-digest>
```

Build Echo separately. Omitting `--service` still selects only the five Debian
services:

```bash
python3.12 packaging/service_bundle.py build \
  --service echo \
  --wheelhouse /path/to/echo-wheelhouse \
  --release-lock release-lock.json \
  --output /path/to/data-tools-bundles \
  --arch amd64
```

The lockfile inputs are Navigator, Yield, and Catalyst root `uv.lock`;
Reactor `product/uv.lock`; and Exchange `product/uv.lock`, which covers the
Product and its local `cyrene-exchange` dependency. The exact versions are
selected by the source commits in `release-lock.json`. This RC supports only
Ubuntu 24.04 x86_64 / Debian `amd64` and Python 3.12, matching the release
lock. Wheelhouse preparation and DEB assembly reject other architectures;
arm64 support requires an explicit supported-environment update. Input
generation may access the network, while `build-deb.sh` consumes only
this complete wheelhouse and never resolves dependencies or falls back to an
index.

The DEB assembly host also needs Python 3.12 venv support
(`python3.12-venv` on Ubuntu); the builder creates an isolated pip installer
environment. The installed package declares `python3 (>= 3.12)`,
`python3 (<< 3.13)`, and `systemd`. Git, uv, and pip are build-time
requirements; the installed service bundles do not use them.

If an immutable release artifact already contains a full wheelhouse, it may be
used only after every service's `source.json` commit and requirements-lock
digest match the Workspace release lock. The previously published CI artifacts
do not include complete offline dependency wheel sets, so prepare the local
wheelhouse with the command above.

## Building and activation / 构建与激活

```bash
CYRENE_SERVICE_WHEELHOUSE=/path/to/wheelhouse \
  ./packaging/build-deb.sh --version 0.1.0-rc.1 --arch amd64 --output ./dist
```

Each schema v2 manifest lists the service, immutable 64-character content version,
entrypoint, service health path, source revision, dependency lock digest, target ABI,
and SHA-256 for every payload file. Debian installation copies these
verified bundles to `/usr/lib/cyrene/services/<service>/releases/<version>`
and creates `active` only when a service has no active release. The native
component updater separates `status`, `check`, `stage`, and `apply` through
`cyrene update --json`. `check` produces a digest-bound plan; `stage` downloads,
verifies, and stores immutable releases without changing active pointers or
acquiring the maintenance gate. `apply` requires explicit confirmation of the
same `planId` and `planDigest`, rechecks the live runtime gate, activates the
release, restarts only that service, and health-checks it. The former
`cyrene upgrade` entry point is disabled so it cannot bypass the update gate.
The post-install script starts units only on first installation.

The embedded component catalog digest pins the bootstrap policy snapshot only.
The Debian package takes that fixed snapshot from
`packaging/component-catalog-bootstrap-v1.json`; the file is kept separate from
the publisher's changing `governance/component-catalog-v1.json`, so catalog-only
generation updates do not require an installer source change.
To inspect or verify later catalog metadata, use `cyrene catalog status` and
`cyrene catalog check --channel stable --latest` (or provide an exact
`--release-id catalog-stable-<40-hex-source-SHA>`). Use `--channel preview`
with `--latest` or a `catalog-preview-<40-hex-source-SHA>` tag to inspect the
preview channel. A check verifies and reports the immutable Workspace catalog
release but does not activate it. Explicitly run
`sudo cyrene catalog import --channel stable --release-id <exact-tag>` to
activate a verified catalog; import requires an exact release tag and changes
updater metadata only, without downloading or installing component releases.

The Workspace catalog publisher emits `component-catalog-v1.json` and the
detached GitHub attestation bundle `component-catalog-v1.json.attestation.jsonl`
in an immutable `catalog-{stable|preview}-<40-hex-source-SHA>` release.
Stable provenance is limited to `main` or `release`; preview provenance is
limited to `develop`. The updater verifies the exact Workspace workflow, source
ref and commit, subject name, raw-byte SHA-256, release tag, and immutable
release assets before import. Catalog schemas for v1 and v2 component manifests
are shipped with the package and validated before activation.

Catalog generations increase strictly. Repeating the same generation and raw
catalog digest is idempotent; a generation collision or rollback is refused.
Checked plans bind both the active generation and raw-byte digest, so a catalog
change invalidates older plans. Import shares the updater lock and waits until
pending maintenance or service recovery intents have been resolved. Its first
activation stages a complete root-owned catalog directory and exposes it with
one atomic rename; the separate root-owned, user-readable state pointer at
`/usr/share/cyrene/component-catalog-state.json` records the active generation
and digest outside the snapshot directory. A missing or damaged snapshot after
that marker is present blocks updater and component startup instead of selecting
the embedded bootstrap catalog.

The manifest health paths are checked on loopback after systemd reports the
unit active:

| Service | Health path | Purpose |
| --- | --- | --- |
| Navigator | `/api/v1/system/status` | Web Host status |
| Yield | `/health` | Product health response |
| Reactor | `/openapi.json` | FastAPI control API is listening |
| Exchange | `/healthz` | Product health response |
| Catalyst | `/` | Product Web UI is serving |
| Echo | `/healthz` | Echo Product API is ready |

Reactor's HTTP check confirms the control API listener only; it does not
establish that an external serving host or model is available.

The first installation enables and starts the broker and five units when
systemd is running; when installed offline, it enables them for the next boot
but cannot start them. Product units receive their own root-managed activity
token through `LoadCredential`. Package upgrades reload unit definitions
without changing enabled state or restarting processes. Component apply leaves
active tasks and loaded models alone: busy or unknown activity blocks apply,
and held workers or allocations must be released by their owning Product before
a core-runtime update. Broker state is journaled under
`/var/lib/cyrene/runtime`; updater plans and transactions are root-private under
`/var/lib/cyrene-updates`. Product data and keys remain outside versioned
release directories. Package removal stops and disables the units, then reloads
systemd after their unit files are removed. Immutable releases and
`/var/lib/cyrene` data are retained for reinstall.

每个 manifest 都记录服务名、不可变 64 位内容版本、前台入口、health path、来源提交、
依赖锁摘要、目标 ABI，以及所有 payload 文件的 SHA-256。首次安装会复制并校验五个
release，只有缺少 `active` 时才设置活动版本。原生组件更新通过 `cyrene update --json`
固定协议分离状态、检查、暂存和应用。暂存只下载、验证并保存不可变版本，不切换活动指针，
也不获取维护门。应用需要对相同 `planId` 和 `planDigest` 显式确认，再通过 Runtime
Maintenance 原子重检准入、激活版本、重启目标服务并执行健康检查。旧 `cyrene upgrade`
入口已禁用，避免绕过维护门。postinst 只在首次安装启动服务。

首次安装且 systemd 正在运行时会启用 broker 和五个 Product 单位，并通过 `LoadCredential`
向每个单位传入独立的活动源 token；离线安装会为下次启动启用单位，但不会启动服务。DEB
升级只重载单位定义，不改变管理员的 enable 状态，也不重启进程。活动任务、闲置 model、
未知活动源或未释放的 worker/allocation 会阻止对应更新；updater 不会取消任务或终止
model。broker journal 位于 `/var/lib/cyrene/runtime`，updater 计划和事务位于 root 私有
的 `/var/lib/cyrene-updates`。密钥和业务数据不随版本目录切换。移除软件包时会停止、禁用
单位，并在 unit 文件移除后重载 systemd；不可变 release 和 `/var/lib/cyrene` 数据会保留。

内嵌 component catalog 的摘要只固定 bootstrap 策略快照。Debian 包从固定文件
`packaging/component-catalog-bootstrap-v1.json` 安装该快照；它与 publisher 持续更新的
`governance/component-catalog-v1.json` 分开保存，因此仅目录 generation 更新无需改变 installer 源码。
查看当前目录可运行
`cyrene catalog status`；候选目录可运行 `cyrene catalog check --channel stable --latest`，
或传入精确的 `--release-id catalog-stable-<40-hex-source-SHA>`。预览通道需显式使用
`--channel preview` 和 `catalog-preview-<40-hex-source-SHA>`。check 会验证并报告
Workspace 不可变目录 release，但不会切换活动目录。只有显式运行
`sudo cyrene catalog import --channel stable --release-id <exact-tag>` 才会激活已验证目录；
导入必须指定精确 tag，且只更新 updater metadata，不会下载或安装组件 release。
Workspace publisher 在不可变 `catalog-{stable|preview}-<40-hex-source-SHA>` release 中发布
`component-catalog-v1.json` 与 detached GitHub attestation bundle
`component-catalog-v1.json.attestation.jsonl`。stable 来源仅允许 `main`/`release`，preview
仅允许 `develop`。导入前会验证固定 Workspace workflow、source ref/commit、subject 名称、原始
字节 SHA-256、release tag 和 immutable assets；catalog v1 与 v2 manifest schemas 随包提供并
在激活前校验。

generation 必须严格递增；相同 generation 与原始摘要可幂等重复，generation 冲突或回滚会拒绝。
check 计划绑定活动 generation 和原始字节摘要，因此目录变化后旧计划失效。目录导入与 updater
共用锁；有未完成维护事务或服务恢复意图时，必须先完成恢复。首次激活会先在活动目录外构造完整
root-owned 目录，再通过一次原子 rename 发布；root-owned 且用户可读的
`/usr/share/cyrene/component-catalog-state.json` 在目录之外记录活动 generation 和摘要。
该标记存在但目录缺失或损坏时，updater 与组件启动都会拒绝回退到内嵌 bootstrap catalog。
后续激活通过原子替换受保护指针完成。
