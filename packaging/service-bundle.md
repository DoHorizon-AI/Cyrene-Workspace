# Linux service bundle inputs / Linux 服务发布包输入

The Debian package includes five offline, per-service Python releases:
Navigator, Yield, Reactor, Exchange, and Catalyst. `build-deb.sh` fails before
creating a package if any release is missing or does not match the immutable
source revision in `release-lock.json`.

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

`source.json` has exactly this shape; each value must describe the same service
and lock used to build the wheels:

```json
{
  "schema_version": 1,
  "service": "navigator",
  "source_repository": "Cyrene-Navigator",
  "source_commit": "<40-character SHA from release-lock.json>",
  "requirements_lock_sha256": "<SHA-256 of requirements.lock>"
}
```

The application distributions required in each lock are `cyrene-navigator`,
`cyrene-yield`, `cyrene-reactor-product`, `cyrene-exchange` plus
`cyrene-exchange-product`, and `cyrene-catalyst`. Navigator also needs the
`scripts/serve-web.py` launcher from its pinned source revision.

## Preparing inputs / 准备输入

Generate the five-service offline wheelhouse from immutable revisions in
`release-lock.json`; do not use whichever sibling checkout happens to be
current. From the Workspace root, run:

```bash
python3.12 packaging/prepare_service_wheelhouse.py \
  --output /path/to/service-wheelhouse
```

The preparation command needs Python 3.12 with pip, Git, uv, and network
access to the pinned source repositories and configured Python package index.
It checks out each exact source SHA in an isolated temporary directory, checks
the service lock, builds application and locked Git/path dependency wheels,
exports a hash-pinned runtime requirements file, and downloads the complete
wheel set. It writes the consumer layout above plus the exact `source.json`
record. Navigator's wheelhouse also includes `scripts/serve-web.py` from its
pinned checkout. The package builder rejects Navigator launchers that do not
accept the secure `--pairing-code-file` option or that print the pairing
secret.

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

Each manifest lists the service, immutable 64-character content version,
entrypoint, service health path, source revision, dependency lock digest, target ABI,
and SHA-256 for every payload file. Debian installation copies these
verified bundles to `/usr/lib/cyrene/services/<service>/releases/<version>`
and creates `active` only when a service has no active release. Package
upgrades stage new immutable releases while retaining the current active
pointer; use `cyrene upgrade <service> --artifact
/usr/share/cyrene/service-artifacts/<service>/<version>` to validate, switch,
health-check, and restart one service. The post-install script starts units
only on first installation.

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

The first installation enables and starts the five units when systemd is
running; when installed offline, it enables them for the next boot but cannot
start them. Package upgrades reload unit definitions and stage bundles without
changing enabled state or restarting processes. Package removal stops and
disables the units, then reloads systemd after their unit files are removed.
Immutable releases and `/var/lib/cyrene` data are retained for reinstall.

每个 manifest 都记录服务名、不可变 64 位内容版本、前台入口、health path、来源提交、
依赖锁摘要、目标 ABI，以及所有 payload 文件的 SHA-256。首次安装会复制并校验五个
release，只有缺少 `active` 时才设置活动版本。DEB 升级会暂存新的不可变版本并保留
当前活动指针；执行 `cyrene upgrade <service> --artifact <bundle-dir>` 才会校验、切换、
健康检查并仅重启指定服务。postinst 只在首次安装启动服务。

首次安装且 systemd 正在运行时会启用并启动五个单位；离线安装会为下次启动启用单位，
但不会启动服务。DEB 升级只重载单位定义并暂存
bundle，不改变管理员的 enable 状态，也不重启进程。移除软件包时会停止、禁用单位，并
在 unit 文件移除后重载 systemd；不可变 release 和 `/var/lib/cyrene` 数据会保留。
