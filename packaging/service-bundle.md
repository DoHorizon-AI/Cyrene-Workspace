# Linux service bundle inputs / Linux 服务发布包输入

The Debian package includes five offline, per-service Python releases:
Navigator, Yield, Reactor, Exchange, and Catalyst. `build-deb.sh` still creates
that complete installation package. The public component producer can build
one service independently for a component release; it does not require a new
five-service DEB for each Product change. Echo has no native Linux Python
bundle in this installation path.

DEB 包离线携带 Navigator、Yield、Reactor、Exchange、Catalyst 五个独立 Python
服务版本。任何服务缺少 wheelhouse，或来源 SHA 与 `release-lock.json` 不匹配时，
`build-deb.sh` 都会明确失败，不会生成只有 unit、没有服务实现的包。

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
`cyrene-exchange-product`, and `cyrene-catalyst`. Navigator also needs the
`scripts/serve-web.py` launcher from its pinned source revision.

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

The manifest health paths are checked on loopback after systemd reports the
unit active:

| Service | Health path | Purpose |
| --- | --- | --- |
| Navigator | `/api/v1/system/status` | Web Host status |
| Yield | `/health` | Product health response |
| Reactor | `/openapi.json` | FastAPI control API is listening |
| Exchange | `/healthz` | Product health response |
| Catalyst | `/` | Product Web UI is serving |

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
