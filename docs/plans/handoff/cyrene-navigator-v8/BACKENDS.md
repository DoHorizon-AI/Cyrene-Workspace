# Cyrene Navigator V8 backend handoff / Cyrene Navigator V8 后端交接

> Historical package snapshot / 历史交付快照：以下 CES 组合仅记录 V8 Alpha 当时的
> 固定后端，不可作为当前实现指南。当前能力业务调用由 Product 直连 Plugins 所有的契约。

这份说明给同事和管理员使用。它描述一个已经验证过的受控 Alpha 后端组合，供已安装
Navigator V8 连接；它不是生产部署器，也不是 Workspace、SSO 或 RBAC 的实现。

This note is for operators and teammates. It describes the controlled Alpha backend
composition used by the installed Navigator V8 package. It is not a production
deployment system and does not complete Workspace, SSO, or RBAC.

```text
Navigator desktop / pinned Harness profile
        │
        ├── Session event API ──> Cyrene persistence (SQLite authority)
        │
        └── Model API ──────────> Exchange Product gateway
                                  │
                                  └── Platform resolver + CES
                                          └── Official model-api connector
                                                  └── standalone vLLM /v1
```

Navigator 也可以直接连接外部 Provider；那是独立的客户端路径，不会自动获得 Exchange 的
路由、usage 或 audit 语义。本文件只描述 adoption proof 和本地 GPU 消费路径所需的
Exchange 组合。

Navigator may also connect to an external Provider directly. That independent client path
does not automatically receive Exchange routing, usage, or audit semantics. This note covers
the Exchange path used by the adoption proof and the local model-consumption slice.

## 版本和已核对入口 / Pins and verified entry points

下面的 revision 是 15:48 UTC 后端真实启动记录使用的固定输入；它们不是 `latest` 约束。

The revisions below are the fixed inputs recorded for the real backend startup at 15:48 UTC;
they are not a request to follow `latest`.

| Component / 组件 | Source entry / 源码入口 | Recorded revision / 记录版本 |
| --- | --- | --- |
| Navigator persistence | `Cyrene-Navigator/scripts/serve-persistence.py`; `pyproject.toml`; `uv.lock` | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` |
| Exchange Product | `Cyrene-Exchange/product/scripts/run_product_vllm_smoke.py`; `product/pyproject.toml`; `product/uv.lock` | `91c509904add26de941e2e14bc3b2b4752233d28` |
| Platform resolver/CES | `Cyrene-Platform/framework/crates/cy-platform-api/src/bin/cyrene-capability-resolver.rs`; `framework/crates/cy-capability-execution-service/src/main.rs`; `Cargo.toml`; `Cargo.lock` | `30272145b9c11df9948465359479c3d95d08a1dc` |
| Official provider | `Cyrene-Plugins-Official/plugins/providers/model-api-connector/plugin.manifest.json` | `261a78fd36c7cb2da85a504a89ec7c9c353ecc19` |

V8 安装包内嵌的 `cy-manifest` 仍固定引用 Platform revision
`185527f82c83700d4f567ac563a393a2e1c18c87`。这是桌面包内部 provenance；它与本次用于
CES/resolver 的 Platform `30272145...` 是两个不同的版本边界，不能把其中一个静默当成另一个。

The V8 package's embedded `cy-manifest` still pins Platform revision
`185527f82c83700d4f567ac563a393a2e1c18c87`. That is package provenance. It is a different
boundary from Platform `30272145...`, which supplies the CES/resolver for this backend run;
do not silently substitute one for the other.

已记录的前置检查是：Platform 的 CES/resolver 使用 `cargo build --locked` 成功；Navigator
和 Exchange Product 项目使用 `uv sync --locked --dry-run` 均无依赖变更。Navigator 和 Exchange
Product 都声明 Python 3.12 运行基线；Exchange 根包还声明 `>=3.11`。Exchange 当前同时
有根目录 `uv.lock` 和 `product/uv.lock`：根锁覆盖 `cyrene-exchange`，Product 锁覆盖
`cyrene-exchange-product` 及其 `product/pyproject.toml` 依赖。

Recorded prerequisites: the Platform CES/resolver build passed with `cargo build --locked`,
and `uv sync --locked --dry-run` reported no changes for Navigator and the Exchange Product
project. Navigator and Exchange Product declare Python 3.12 as their working baseline; the
Exchange root package also declares `>=3.11`. The current Exchange checkout has both a root
`uv.lock` for `cyrene-exchange` and a `product/uv.lock` for `cyrene-exchange-product` and its
Product dependencies.

## 组件职责 / Component responsibilities

### 1. Cyrene persistence

入口是 `Cyrene-Navigator/scripts/serve-persistence.py`，应用工厂是
`cyrene_navigator.persistence.create_persistence_app`。启动器：

The entry point is `Cyrene-Navigator/scripts/serve-persistence.py`; the application factory
is `cyrene_navigator.persistence.create_persistence_app`. The launcher:

- 从 `--principal-config` 读取严格校验的 `principals` 列表；每项包含 `token_env`、`actor_id`、`workspace_ids` 和可选 `can_takeover`。
- 只从 `token_env` 指定的进程环境变量取 bearer credential，不接受 token CLI 参数，也不把 token 写入 principal 文件或 readiness 输出。
- 将 `(workspace, session)` 事件写入指定 `--database`；持久层使用 SQLite WAL、`synchronous=FULL`、服务端 lease/epoch 和 append-only 上游事件。
- 默认绑定 `127.0.0.1`，`--port 0` 让操作系统分配端口，并只输出不含凭据的 host/port JSON。

- Reads a strict `principals` list from `--principal-config`; each row contains `token_env`,
  `actor_id`, `workspace_ids`, and optional `can_takeover`.
- Reads the bearer credential only from the process environment variable named by `token_env`.
  It has no token CLI argument and does not put token values in the principal file or readiness output.
- Stores `(workspace, session)` events in the explicit `--database` using SQLite WAL,
  `synchronous=FULL`, server-side lease/epoch fencing, and append-only upstream events.
- Binds to `127.0.0.1` by default. `--port 0` requests an OS-selected port and the only startup
  banner is credential-free host/port JSON.

这份 principal 配置是 Phase 0 的受信 bootstrap 映射，不是用户目录、Identity Provider、
SSO 或完整 RBAC。`workspace_ids` 是服务端授权边界；请求 JSON 自报的 actor/workspace
不能扩大权限。所有 Navigator 设备应指向同一个受管持久目录中的同一个 SQLite authority，
而不是各自维护一份可写会话历史。

This principal file is a Phase 0 trusted bootstrap mapping, not a user directory, Identity
Provider, SSO, or full RBAC system. `workspace_ids` is enforced by the service; actor/workspace
values supplied in request JSON do not grant access. Navigator devices that share Sessions must
use the same managed SQLite authority, rather than maintaining separate writable histories.

一个无密钥的配置形状如下；真实 credential 由 secret provider 或隐藏输入注入环境：

The shape of a credential-free principal file is:

```json
{
  "principals": [
    {
      "token_env": "CYRENE_PERSISTENCE_TOKEN",
      "actor_id": "operator-actor",
      "workspace_ids": ["workspace-dev"],
      "can_takeover": false
    }
  ]
}
```

### 2. Exchange Product gateway

入口是 `Cyrene-Exchange/product/scripts/run_product_vllm_smoke.py`。`--serve` 模式会：

The entry point is `Cyrene-Exchange/product/scripts/run_product_vllm_smoke.py`. In `--serve`
mode it:

1. 检查 Platform resolver、CES 二进制和 Official provider manifest。
2. 启动一次 CES 子进程，并通过 Platform resolver/CES/Official connector 组成执行路径。
3. 使用 Exchange Product SQLite 真源幂等创建或复用 `GatewayEndpoint` 与 `GatewayRoute`；复用同一 `--database` 时 route/endpoint 身份可保持稳定。
4. 在 loopback 上启动 `create_reference_server`，默认由 `--listen-port 0` 分配端口；ready JSON/`--ready-file` 只包含 URL、资源引用和状态元数据，不包含 bearer 或 provider key。
5. 收到 SIGINT/SIGTERM 时关闭 HTTP server、CES client、SQLite store 和 CES 子进程。

1. Checks the Platform resolver, CES binary, and Official provider manifest.
2. Starts a CES child and composes the Platform resolver/CES/Official connector execution path.
3. Idempotently creates or reuses `GatewayEndpoint` and `GatewayRoute` in the Exchange Product
   SQLite authority; reusing `--database` preserves the endpoint/route identity.
4. Starts `create_reference_server` on loopback, with `--listen-port 0` selecting an ephemeral
   port. Readiness JSON and `--ready-file` contain URL, resource references, and status metadata,
   never bearer or provider key values.
5. On SIGINT/SIGTERM, closes the HTTP server, CES client, SQLite store, and CES child process.

Exchange Product 负责 route/endpoint 选择和请求账本；Platform 只执行已选择的 capability，
不成为第二套路由 authority。此入口是可复跑的 Alpha Product proof/reference transport，
不是进程监管器、TLS terminator、GPU scheduler 或生产 HA gateway。

Exchange Product owns endpoint/route selection and request accounting; Platform executes the
selected capability and is not a second routing authority. This is a repeatable Alpha Product
proof/reference transport, not a process supervisor, TLS terminator, GPU scheduler, or
production HA gateway.

### 3. 独立 vLLM / Standalone vLLM

`run_product_vllm_smoke.py` 不安装、启动或管理 vLLM。它要求已有的 vLLM 进程提供
OpenAI-compatible `/v1` API；脚本默认使用
`http://127.0.0.1:19180/v1`，也可通过已核对的 `CYRENE_VLLM_BASE_URL` 或
`--vllm-base-url` 指定，模型通过 `CYRENE_VLLM_MODEL` 或 `--model` 指定。

`run_product_vllm_smoke.py` does not install, start, or manage vLLM. A separate vLLM process
must already expose an OpenAI-compatible `/v1` API. The script defaults to
`http://127.0.0.1:19180/v1`; `CYRENE_VLLM_BASE_URL` or `--vllm-base-url` selects another
endpoint, and `CYRENE_VLLM_MODEL` or `--model` selects the model.

本文件不把手工启动 vLLM 记作 Reactor 部署验收。模型文件、CUDA、NVIDIA GPU、vLLM
参数和健康检查属于独立的 Reactor/Platform slice；Exchange 只消费这个兼容端点。

This note does not count manually starting vLLM as a Reactor deployment acceptance. Model
files, CUDA, NVIDIA GPU, vLLM options, and serving health belong to the separate
Reactor/Platform slice; Exchange consumes the compatible endpoint.

## 构建和启动顺序 / Build and startup sequence

以下命令使用 checkout 的项目目录；尖括号内容是操作员自己的路径或非敏感标识。命令中
没有 secret 值。

The commands below use the repository checkout directories. Angle-bracket values are
operator-owned paths or non-secret identifiers; no secret value is shown.

Use the exact commits in `release-lock.json`, with the relative repository layout
below. The Exchange Product lock includes an editable Platform Python SDK path;
`--platform-root` changes runtime lookup, but does not change that locked install
path. Install locked dependencies before starting services.

按 `release-lock.json` 检出精确提交，并保留下列相对目录结构。Exchange Product lock 包含
Platform Python SDK 的相对 editable 路径；`--platform-root` 只改变运行时查找，不会改变
锁定依赖的安装路径。先安装 locked dependencies，再启动服务。

```text
Cyrene/
├── Cyrene-Platform/
├── Cyrene-Plugins-Official/
└── Services/
    ├── Cyrene-Exchange/
    └── Cyrene-Navigator/
```


### 1. 验证并构建 Platform / Verify and build Platform

```bash
cd <Cyrene-Platform-checkout>
cargo build --locked -p cy-platform-api --bin cyrene-capability-resolver
cargo build --locked -p cy-capability-execution-service --bin cyrene-capability-execution-service
```

Product script 默认在 Platform checkout 的 `target/debug/` 查找这两个 binary；若 checkout
不在相邻目录，使用脚本的 `--platform-root`。同时让 `--plugins-root` 指向含有
`plugins/providers/model-api-connector/plugin.manifest.json` 的 Official checkout。

By default the Product script looks for both binaries under `target/debug/` in the Platform
checkout. Use `--platform-root` when it is elsewhere, and set `--plugins-root` to the Official
checkout containing `plugins/providers/model-api-connector/plugin.manifest.json`.

### 2. 准备并启动 persistence / Prepare and start persistence

先使用受管且权限受限的持久目录，例如管理员为 SQLite 和 readiness/log 文件分别准备的
Alpha 数据目录；不要把数据库放在临时目录，也不要复制一份数据库作为第二个写入 authority。

Use a managed, access-restricted persistent directory for the SQLite and operational files.
Do not use a temporary directory for the database or copy it to create a second writable
Session authority.

```bash
cd <Cyrene-Navigator-checkout>
uv sync --locked

# Hidden input or a secret provider; never paste the value into the command line.
read -r -s CYRENE_PERSISTENCE_TOKEN
export CYRENE_PERSISTENCE_TOKEN

uv run --locked --project . python scripts/serve-persistence.py \
  --database <private-persistence-dir>/navigator-sessions.sqlite3 \
  --principal-config <private-config-dir>/principals.json \
  --host 127.0.0.1 \
  --port 0 \
  --lease-seconds 120
```

`principals.json` 中的 `token_env` 必须与环境变量名一致。操作员可为每个受信 principal
选择自己的非敏感 `actor_id` 和 `workspace_ids`；同一个 Workspace 的多个设备必须获得
各自合法 credential，并把客户端 device ID 配置为稳定且不同的值。启动 banner 记录的
loopback 端口应交给受控 relay 或本机 Navigator 配置。

The `token_env` names in `principals.json` must match the process environment. Operators choose
the non-secret `actor_id` and `workspace_ids` for each trusted principal. Devices sharing one
Workspace need authorized credentials and stable distinct client device IDs. Pass the advertised
loopback port to the controlled relay or local Navigator configuration.

### 3. 启动 Exchange Product gateway / Start the Exchange Product gateway

先确认独立 vLLM 已经监听其 OpenAI-compatible URL。Exchange bearer credential 必须通过
`CYRENE_EXCHANGE_BEARER_TOKEN` 注入；provider key 如有需要通过已核对的
`CYRENE_VLLM_API_KEY` 注入。不要把任何 key 放进 CLI 参数、提交文件或 ready 文件。

Confirm that standalone vLLM is already listening at its OpenAI-compatible URL. Inject the
Exchange bearer credential through `CYRENE_EXCHANGE_BEARER_TOKEN`; when needed, inject the
provider key through the verified `CYRENE_VLLM_API_KEY` environment variable. Do not put any key
in CLI arguments, committed files, or readiness files.

```bash
cd <Cyrene-Exchange-checkout>
uv sync --locked --project product

# Hidden input or a secret provider; the value is intentionally omitted.
read -r -s CYRENE_EXCHANGE_BEARER_TOKEN
export CYRENE_EXCHANGE_BEARER_TOKEN

export CYRENE_VLLM_BASE_URL=http://127.0.0.1:19180/v1
export CYRENE_VLLM_MODEL=cyrene-proof-text
export CYRENE_EXCHANGE_ACTOR_ID=operator-actor
export CYRENE_EXCHANGE_WORKSPACE_ID=workspace-dev
export CYRENE_EXCHANGE_CREDENTIAL_REF=credential-operator

uv run --locked --project product python product/scripts/run_product_vllm_smoke.py \
  --serve \
  --database <private-exchange-dir>/exchange-product.sqlite3 \
  --listen-port 0 \
  --ready-file <private-exchange-dir>/exchange-product.ready.json
```

`--serve` 必须有持久 `--database`，且不能与 `--tool-smoke` 同时使用。要做一次性真实
请求、stream、tool call/tool result smoke，可在另一份受控数据库上运行：

`--serve` requires a persistent `--database` and cannot be combined with `--tool-smoke`. For
one-shot unary, streaming, and tool-call/tool-result smoke, use a separate controlled database:

```bash
uv run --locked --project product python product/scripts/run_product_vllm_smoke.py \
  --vllm-base-url http://127.0.0.1:19180/v1 \
  --model cyrene-proof-text \
  --tool-smoke
```

这个 Product 脚本在运行时创建临时 `0600` binding 文件，把 vLLM 配置交给 CES；持久
Exchange SQLite 保存 endpoint/route 和受控 credential reference，不保存 raw provider
token。ready JSON 中的 URL 是内部 loopback 地址；Navigator 远程连接时应使用下面的
HTTPS relay 地址。

The Product script creates a temporary `0600` binding file for the CES invocation. Persistent
Exchange SQLite stores endpoint/route state and the controlled credential reference, not the
raw provider token. The URL in readiness JSON is an internal loopback address; remote Navigator
clients must use the HTTPS relay described below.

### 4. 配置已安装 Navigator / Configure the installed Navigator

Navigator 安装包不会启动 persistence、Exchange、Reactor 或 vLLM。通过现有 connector 或
组织的受控配置提供：`CYRENE_EXCHANGE_URL`、`CYRENE_EXCHANGE_TOKEN`、
`CYRENE_PERSISTENCE_URL`、`CYRENE_SESSION_TOKEN`、`CYRENE_WORKSPACE_ID`、
`CYRENE_HARNESS_MODEL`；`CYRENE_ACCOUNT_PROFILE_ID` 和 `CYRENE_DEVICE_ID` 用于本地
客户端命名空间。两个 token 只应由 connector 以隐藏输入或 secret provider 注入子进程。

The Navigator installer does not start persistence, Exchange, Reactor, or vLLM. Use the existing
connector or an organization-controlled configuration to provide `CYRENE_EXCHANGE_URL`,
`CYRENE_EXCHANGE_TOKEN`, `CYRENE_PERSISTENCE_URL`, `CYRENE_SESSION_TOKEN`,
`CYRENE_WORKSPACE_ID`, and `CYRENE_HARNESS_MODEL`; `CYRENE_ACCOUNT_PROFILE_ID` and
`CYRENE_DEVICE_ID` identify local client namespaces. The two service tokens should enter the
child process only through hidden input or a secret provider.

## HTTPS 入口和网络边界 / HTTPS edge and network boundary

这两个 launcher 本身都提供明文 HTTP：persistence 使用 Uvicorn socket，Exchange 使用
stdlib `ThreadingHTTPServer`；它们默认只绑定 loopback，也没有证书、hostname、CA、firewall
或公网监听配置。独立 vLLM 默认同样是本机 HTTP。

Both launchers serve plain HTTP themselves: persistence uses an Uvicorn socket and Exchange
uses the stdlib `ThreadingHTTPServer`. They default to loopback and do not configure certificates,
hostnames, CAs, firewalls, or public listeners. Standalone vLLM is also local HTTP by default.

面向第二台设备或远程 Navigator 时，受控 HTTPS relay/reverse proxy 才负责：

For a second device or a remote Navigator, the controlled HTTPS relay/reverse proxy owns:

- 外部 bind、hostname、TLS certificate、受信 CA 和 HTTP→loopback forwarding；
- firewall/allowlist、来源策略以及把 guest/device 限制到指定 backend port；
- 对外公布的 HTTPS URL 与证书轮换。

- the external bind, hostname, TLS certificate, trusted CA, and HTTP-to-loopback forwarding;
- firewall/allowlist and origin policy, including limiting a guest/device to the intended backend port;
- the public HTTPS URL and certificate rotation.

后端仍必须执行各自的 bearer authentication 和 Workspace 检查；relay 不能通过信任请求
JSON 自报 actor/workspace 来替代后端 authority。Navigator 使用系统 CA 校验，不能用
`SkipCertificateCheck` 或关闭 TLS 验证。本文不指定 relay 的私有命令或证书路径，因为它们
属于部署环境，不能从两个 launcher 的源码推断。

The backends still enforce their own bearer authentication and Workspace checks; the relay
cannot replace backend authority by trusting actor/workspace fields from request JSON. Navigator
uses system CA verification; do not use `SkipCertificateCheck` or disable TLS validation. This
note does not prescribe a private relay command or certificate path because those belong to the
deployment environment and cannot be inferred from either launcher.

## Alpha 边界 / Alpha limits

- 这是 persistence + Exchange Product + Platform CES/resolver + Official connector + 独立 vLLM 的受控组合；它不是统一控制面板。
- Reactor 仍拥有 Deployment、Loaded Model、Endpoint readiness；本文件的 Product route 连接一个已存在的兼容端点，不能替代 Reactor P1 验收。
- Persistence principal 文件不是 SSO/RBAC；Exchange 的完整 quota、cost、组织策略和密钥轮换仍属于后续 Workspace/Identity/Exchange 工作。
- SQLite 路径、HTTPS relay、进程监管、备份和恢复策略由管理员负责；当前脚本不提供生产 HA、自动 GPU placement 或多节点调度。
- 所有成功状态都必须由实际服务响应、持久数据库和真实 provider/CES 路径复核；启动 banner 或 prompt accepted 不等于模型回答、部署或端到端可用。

- This is a controlled composition of persistence, Exchange Product, Platform CES/resolver,
  the Official connector, and standalone vLLM; it is not a universal control plane.
- Reactor still owns Deployment, Loaded Model, and Endpoint readiness. The Product route here
  consumes an existing compatible endpoint and does not prove the Reactor P1 slice.
- The persistence principal file is not SSO/RBAC. Full quota, cost, organization policy, and
  credential rotation remain later Workspace/Identity/Exchange work.
- Administrators own SQLite placement, HTTPS relay, process supervision, backup, and recovery;
  these scripts do not provide production HA, automatic GPU placement, or multi-node scheduling.
- Acceptance requires real service responses, durable database state, and the real provider/CES
  path. A startup banner or an accepted prompt is not proof of a completed model turn, deployment,
  or end-to-end usability.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene Navigator V8 后端交接

> **历史安装包快照：** 以下 CES 组合只记录 V8 Alpha 当时固定的后端，不是当前实现指南。当前能力的业务调用由 Product 直接连接 Plugins 所有的契约。

本文面向操作员和同事，说明已安装 Navigator V8 所使用、且已验证的受控 Alpha 后端组合。它不是生产部署系统，也没有完成 Workspace、SSO 或 RBAC 的实现。

```text
Navigator 桌面端 / 固定 Harness profile
        │
        ├── Session event API ──> Cyrene persistence（SQLite 权威）
        │
        └── Model API ──────────> Exchange Product gateway
                                  │
                                  └── Platform resolver + CES
                                          └── Official model-api connector
                                                  └── 独立 vLLM /v1
```

Navigator 也可以直接连接外部 Provider。这条独立客户端路径不会自动获得 Exchange 的路由、usage 或 audit 语义。本文只说明采用证明和本地模型消费所需的 Exchange 路径。

## 版本与已核对入口

下列 revision 是 15:48 UTC 后端真实启动记录中的固定输入，并不表示要跟随 `latest`。

| 组件 | 源码入口 | 记录版本 |
| --- | --- | --- |
| Navigator persistence | `Cyrene-Navigator/scripts/serve-persistence.py`；`pyproject.toml`；`uv.lock` | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` |
| Exchange Product | `Cyrene-Exchange/product/scripts/run_product_vllm_smoke.py`；`product/pyproject.toml`；`product/uv.lock` | `91c509904add26de941e2e14bc3b2b4752233d28` |
| Platform resolver/CES | `Cyrene-Platform/framework/crates/cy-platform-api/src/bin/cyrene-capability-resolver.rs`；`framework/crates/cy-capability-execution-service/src/main.rs`；`Cargo.toml`；`Cargo.lock` | `30272145b9c11df9948465359479c3d95d08a1dc` |
| Official provider | `Cyrene-Plugins-Official/plugins/providers/model-api-connector/plugin.manifest.json` | `261a78fd36c7cb2da85a504a89ec7c9c353ecc19` |

V8 安装包内嵌的 `cy-manifest` 仍固定引用 Platform revision `185527f82c83700d4f567ac563a393a2e1c18c87`。这是桌面包的 provenance，与本次 CES/resolver 后端所用的 Platform `30272145...` 是两个版本边界，不得静默互换。

前置检查记录显示：Platform CES/resolver 通过 `cargo build --locked` 构建；Navigator 和 Exchange Product 分别执行 `uv sync --locked --dry-run` 后均无依赖变更。Navigator 与 Exchange Product 声明以 Python 3.12 为运行基线；Exchange 根包另声明 `>=3.11`。Exchange checkout 同时有根目录 `uv.lock` 和 `product/uv.lock`：根锁对应 `cyrene-exchange`，Product 锁对应 `cyrene-exchange-product` 及 `product/pyproject.toml` 依赖。

## 组件职责

### 1. Cyrene persistence

入口为 `Cyrene-Navigator/scripts/serve-persistence.py`，应用工厂为 `cyrene_navigator.persistence.create_persistence_app`。启动器从 `--principal-config` 读取严格校验的 `principals` 列表，每项包含 `token_env`、`actor_id`、`workspace_ids` 和可选的 `can_takeover`。它只从 `token_env` 指定的进程环境变量读取 bearer credential，没有 token CLI 参数，也不会把 token 写入 principal 文件或 readiness 输出。

启动器将 `(workspace, session)` 事件写到指定 `--database`，使用 SQLite WAL、`synchronous=FULL`、服务端 lease/epoch fencing 和只追加的上游事件。默认绑定 `127.0.0.1`；`--port 0` 请求操作系统分配端口，启动时只输出不含凭据的 host/port JSON。

principal 文件只是 Phase 0 的受信 bootstrap 映射，不是用户目录、Identity Provider、SSO 或完整 RBAC。`workspace_ids` 由服务端执行授权；请求 JSON 自报的 actor/workspace 不能授予权限。共享 Session 的 Navigator 设备必须使用同一受管 SQLite authority，不能各自维护可写会话历史。

无密钥配置示例：真实 credential 由 secret provider 或隐藏输入注入环境。

```json
{
  "principals": [
    {
      "token_env": "CYRENE_PERSISTENCE_TOKEN",
      "actor_id": "operator-actor",
      "workspace_ids": ["workspace-dev"],
      "can_takeover": false
    }
  ]
}
```

### 2. Exchange Product gateway

入口为 `Cyrene-Exchange/product/scripts/run_product_vllm_smoke.py`。`--serve` 模式先检查 Platform resolver、CES binary 和 Official provider manifest；启动 CES 子进程并组合 Platform resolver/CES/Official connector 执行路径；通过 Exchange Product SQLite authority 幂等创建或复用 `GatewayEndpoint`、`GatewayRoute`，重复使用同一 `--database` 可保持 endpoint/route identity。随后在 loopback 启动 `create_reference_server`，默认由 `--listen-port 0` 分配临时端口。ready JSON 和 `--ready-file` 只含 URL、资源引用及状态 metadata，不含 bearer 或 provider key。收到 SIGINT/SIGTERM 时关闭 HTTP server、CES client、SQLite store 和 CES 子进程。

Exchange Product 拥有 endpoint/route 选择和请求账本；Platform 执行已经选定的 capability，不是第二套路由 authority。此入口是可重复运行的 Alpha Product proof/reference transport，不是进程监管器、TLS terminator、GPU scheduler 或生产级 HA gateway。

### 3. 独立 vLLM

`run_product_vllm_smoke.py` 不安装、启动或管理 vLLM。必须已有独立 vLLM 进程提供兼容 OpenAI 的 `/v1` API。脚本默认地址是 `http://127.0.0.1:19180/v1`；可通过已核对的 `CYRENE_VLLM_BASE_URL` 或 `--vllm-base-url` 指定端点，通过 `CYRENE_VLLM_MODEL` 或 `--model` 指定模型。

手动启动 vLLM 不算 Reactor 部署验收。模型文件、CUDA、NVIDIA GPU、vLLM 参数和服务健康属于独立的 Reactor/Platform slice；Exchange 只消费兼容端点。

## 构建与启动顺序

命令使用各项目 checkout 目录。尖括号表示操作员自有路径或非敏感标识；命令不含 secret。按 `release-lock.json` 检出精确提交并保留以下相对目录布局：

```text
Cyrene/
├── Cyrene-Platform/
├── Cyrene-Plugins-Official/
└── Services/
    ├── Cyrene-Exchange/
    └── Cyrene-Navigator/
```

Exchange Product lock 使用相对路径的 editable Platform Python SDK；`--platform-root` 只改变运行时查找位置，不会改变锁定安装路径。启动服务前先安装 locked dependencies。

### 1. 验证并构建 Platform

```bash
cd <Cyrene-Platform-checkout>
cargo build --locked -p cy-platform-api --bin cyrene-capability-resolver
cargo build --locked -p cy-capability-execution-service --bin cy-capability-execution-service
```

Product script 默认从 Platform checkout 的 `target/debug/` 查找这两个 binary；checkout 不在相邻目录时使用 `--platform-root`。同时将 `--plugins-root` 指向含有 `plugins/providers/model-api-connector/plugin.manifest.json` 的 Official checkout。

### 2. 准备并启动 persistence

使用受管且权限受限的持久目录，例如管理员为 SQLite 与 readiness/log 文件分别准备的 Alpha 数据目录。数据库不得放入临时目录，也不得复制数据库形成第二个可写 Session authority。

```bash
cd <Cyrene-Navigator-checkout>
uv sync --locked
# 从隐藏输入或 secret provider 读取，不要将值粘贴到命令行。
read -r -s CYRENE_PERSISTENCE_TOKEN
export CYRENE_PERSISTENCE_TOKEN
uv run --locked --project . python scripts/serve-persistence.py \
  --database <private-persistence-dir>/navigator-sessions.sqlite3 \
  --principal-config <private-config-dir>/principals.json \
  --host 127.0.0.1 \
  --port 0 \
  --lease-seconds 120
```

`principals.json` 的 `token_env` 必须与环境变量名一致。操作员为每个受信 principal 选择非敏感的 `actor_id` 和 `workspace_ids`。共享 Workspace 的设备必须各有合法 credential，并配置稳定且互不相同的 client device ID。启动 banner 所示 loopback 端口应交给受控 relay 或本机 Navigator 配置。

### 3. 启动 Exchange Product gateway

先确认独立 vLLM 已在兼容 OpenAI 的 URL 监听。Exchange bearer credential 通过 `CYRENE_EXCHANGE_BEARER_TOKEN` 注入；需要 provider key 时通过已核对的 `CYRENE_VLLM_API_KEY` 注入。任何 key 都不能出现在 CLI 参数、提交文件或 ready 文件中。

```bash
cd <Cyrene-Exchange-checkout>
uv sync --locked --project product
# 从隐藏输入或 secret provider 读取；这里不显示实际值。
read -r -s CYRENE_EXCHANGE_BEARER_TOKEN
export CYRENE_EXCHANGE_BEARER_TOKEN
export CYRENE_VLLM_BASE_URL=http://127.0.0.1:19180/v1
export CYRENE_VLLM_MODEL=cyrene-proof-text
export CYRENE_EXCHANGE_ACTOR_ID=operator-actor
export CYRENE_EXCHANGE_WORKSPACE_ID=workspace-dev
export CYRENE_EXCHANGE_CREDENTIAL_REF=credential-operator
uv run --locked --project product python product/scripts/run_product_vllm_smoke.py \
  --serve \
  --database <private-exchange-dir>/exchange-product.sqlite3 \
  --listen-port 0 \
  --ready-file <private-exchange-dir>/exchange-product.ready.json
```

`--serve` 必须使用持久 `--database`，且不能与 `--tool-smoke` 同时运行。若要对真实服务做一次性 unary、streaming、tool call/tool result smoke，应使用另一份受控数据库：

```bash
uv run --locked --project product python product/scripts/run_product_vllm_smoke.py \
  --vllm-base-url http://127.0.0.1:19180/v1 \
  --model cyrene-proof-text \
  --tool-smoke
```

Product script 运行时创建临时 `0600` binding 文件，把 vLLM 配置交给 CES。Exchange SQLite 持久保存 endpoint/route 和受控 credential reference，不保存 raw provider token。ready JSON 中 URL 是内部 loopback 地址；Navigator 远程连接必须使用 HTTPS relay 地址。

### 4. 配置已安装 Navigator

Navigator 安装包不会启动 persistence、Exchange、Reactor 或 vLLM。通过现有 connector 或组织受控配置提供 `CYRENE_EXCHANGE_URL`、`CYRENE_EXCHANGE_TOKEN`、`CYRENE_PERSISTENCE_URL`、`CYRENE_SESSION_TOKEN`、`CYRENE_WORKSPACE_ID`、`CYRENE_HARNESS_MODEL`。`CYRENE_ACCOUNT_PROFILE_ID` 和 `CYRENE_DEVICE_ID` 用于本地客户端命名空间。两个 token 只能通过隐藏输入或 secret provider 注入子进程。

## HTTPS 入口和网络边界

两个 launcher 自身提供明文 HTTP：persistence 使用 Uvicorn socket，Exchange 使用标准库 `ThreadingHTTPServer`。默认只绑定 loopback，未配置证书、hostname、CA、firewall 或公网监听。独立 vLLM 默认也只提供本机 HTTP。

第二台设备或远程 Navigator 所用的受控 HTTPS relay/reverse proxy 负责外部 bind、hostname、TLS certificate、受信 CA、HTTP 到 loopback 的转发；负责 firewall/allowlist、origin 策略及将 guest/device 限制到指定 backend port；也负责公布 HTTPS URL 和证书轮换。

后端仍须各自执行 bearer authentication 和 Workspace 检查；relay 不能靠信任请求 JSON 自报 actor/workspace 来取代后端 authority。Navigator 使用系统 CA 验证，不能使用 `SkipCertificateCheck` 或关闭 TLS 验证。relay 私有命令和证书路径属于部署环境，不能从两个 launcher 源码推断，因此本文不指定。

## Alpha 边界

- 这是 persistence、Exchange Product、Platform CES/resolver、Official connector 与独立 vLLM 的受控组合，不是统一控制面板。
- Reactor 仍拥有 Deployment、Loaded Model 和 Endpoint readiness。本文件里的 Product route 连接既有兼容端点，不等于 Reactor P1 验收。
- Persistence principal 文件不构成 SSO/RBAC。完整 quota、cost、组织策略和密钥轮换仍属于后续 Workspace/Identity/Exchange 工作。
- SQLite 路径、HTTPS relay、进程监管、备份和恢复策略由管理员负责；当前脚本不提供生产 HA、自动 GPU placement 或多节点调度。
- 必须以真实服务响应、持久数据库和真实 provider/CES 路径复核成功状态。启动 banner 或 prompt accepted 不等于模型回答、部署或端到端可用。
