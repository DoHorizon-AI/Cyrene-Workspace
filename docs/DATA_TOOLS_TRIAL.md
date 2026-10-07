# Linux data-tools trial / Linux 数据工具试用

This operator launcher starts Catalyst and Echo with five real local Plugin
endpoints: document parsing, knowledge preparation, dataset generation, dataset
preparation, and exact-match evaluation. Both Products share an artifact root;
their SQLite databases remain separate. The default profile exposes only the
two Product APIs and does not start the Platform daemon.

## Start the APIs

The launcher expects the trial source tree to contain `workspace/`,
`catalyst/`, `echo/`, `plugins/`, and optionally `client/`. Python 3.12 and `uv`
are required. From the trial source root:

```bash
python3.12 workspace/packaging/data_tools_trial.py start --source-root "$PWD"
```

By default, Catalyst listens at `http://127.0.0.1:8014` and Echo at
`http://127.0.0.1:8094`. `GET /healthz` on each URL is the readiness check.
Call Product routes under each base URL's `/api/v1/` path. When using the
Navigator Web Host, Catalyst routes use `/api/v1/catalyst/...` and Echo routes
use `/api/v1/echo/...`; the UI proxy removes that Product prefix and forwards to
the corresponding Product `/api/v1/` route.
The launcher records the actual Plugin `direct_plugin_ready` events and their
opaque local `connection_ref` values in `runtime.json`; Products receive those
same values through their declared connection-reference environment variables.
The reference strings are discovered from the running Plugin endpoints, never
manufactured by the launcher.

Runtime data lives under
`${XDG_STATE_HOME:-~/.local/state}/cyrene/data-tools-trial` by default:

```text
<state-dir>/runtime.json
<state-dir>/artifacts/             # shared by Catalyst and Echo
<state-dir>/catalyst/catalyst.sqlite3
<state-dir>/echo/echo.sqlite3
<state-dir>/logs/
```

Use the same `--state-dir` for subsequent operations. Separate trial instances
can use separate state directories and API ports:

```bash
python3.12 workspace/packaging/data_tools_trial.py start \
  --source-root "$PWD" \
  --state-dir "$HOME/.local/state/cyrene/data-tools-trial-secondary" \
  --catalyst-port 8114 --echo-port 8194
```

Each instance owns its databases and artifact root. Plugin endpoints use
OS-assigned loopback ports unless `--plugin-port-base` is set.

## Optional Client and Navigator UI

The UI profile also starts the current Client Control service, Vite development
server, and canonical Navigator Web Host. It requires Node.js 24 or newer,
installed Client dependencies (`cd client && npm ci`), and the Navigator source
checkout. The default Navigator checkout is
`<Cyrene-root>/Cyrene-Services/Cyrene-Navigator`; pass `--navigator-root` to
select another checkout.

```bash
python3.12 workspace/packaging/data_tools_trial.py start \
  --source-root "$PWD" --with-ui
```

The UI defaults to Client `http://127.0.0.1:5180`, Control
`http://127.0.0.1:5280`, and Navigator Web Host `http://127.0.0.1:8100`.
After all three surfaces are ready, the launcher prints a one-time Navigator
pairing code. Enter that code in the Client's existing pairing flow within
15 minutes. The code is not written to `runtime.json` or the logs. The Web Host owns the Product API
proxy and stores its bearer in server memory; the Control process and browser
do not receive `CYRENE_DATA_TOOLS_TOKEN`. The UI is a loopback development
surface and cannot be combined with `--allow-remote`.

## Remote API binding

The default bind is loopback and needs no trial bearer. A non-loopback API bind
requires an explicit high-entropy token (at least 32 printable ASCII bytes),
`--allow-remote`, and an independently configured TLS terminator. Supply the
token through the launcher process environment; do not put it in arguments,
`.env` files, `runtime.json`, or shell tracing output:

```bash
read -rsp "Trial API bearer: " CYRENE_DATA_TOOLS_TOKEN
printf '\n'
export CYRENE_DATA_TOOLS_TOKEN
python3 workspace/packaging/data_tools_trial.py start \
  --source-root "$PWD" --host 0.0.0.0 --allow-remote
unset CYRENE_DATA_TOOLS_TOKEN
```

Optional `CYRENE_DATA_TOOLS_ORGANIZATION_ID` and
`CYRENE_DATA_TOOLS_WORKSPACE_ID` values scope the instance principal; defaults
are `data-tools-trial` and `data-tools`. The launcher does not create or
configure TLS, firewall rules, DNS, or a public reverse proxy.

## Stop, back up, and remove data

Stop preserves the databases and artifacts. Back up only after stopping so the
SQLite files are consistent. Clean permanently deletes the stopped instance and
requires an explicit confirmation flag:

```bash
python3.12 workspace/packaging/data_tools_trial.py status --state-dir <state-dir>
python3.12 workspace/packaging/data_tools_trial.py stop --state-dir <state-dir>
python3.12 workspace/packaging/data_tools_trial.py backup --state-dir <state-dir>
python3.12 workspace/packaging/data_tools_trial.py clean \
  --state-dir <state-dir> --confirm-data-loss
```

`backup` writes a mode-0600 archive by default beside the state directory under
`data-tools-trial-backups/`; pass `--output <path>` to select another new path.
The archive contains shared artifacts and the two Product data directories, not
process records or runtime credentials.

## Optional OCR and model generation

PDF parsing uses the local model-free path by default, and DOCX parsing uses its
local pipeline. Operators may install OCR/model dependencies and downloaded
Docling model artifacts separately, then set
`CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH` in the launcher environment. Install
the optional parser dependencies into the active trial Plugin environment and
download models as a separate operator action:

```bash
python -m pip install -e 'plugins/plugins/tools/document-parsing[models]'
docling-tools models download --output-dir <model-directory>
export CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH=<model-directory>
```

With the default source-root layout, activate `<source-root>/.venv` first when
the prewarmed environment is present; otherwise use
`<state-dir>/venvs/plugins/bin/activate`. Restart the trial after changing
dependencies or model settings.

The dataset-generation Plugin also starts without a model provider. Manual SFT
preparation remains available; a requested QA-generation operation reports the
missing `model.provider.v1` binding until an operator configures a provider.
The trial launcher does not invent a model endpoint or report generation as
ready when one is absent. To configure QA generation, first run a local
`model.provider.v1` Plugin and use its resolved local gRPC or Unix connection
reference. The JSON file follows the Plugin configuration schema:

```json
{
  "model_endpoint": "grpc://127.0.0.1:19080",
  "model": "scripted-or-local-model",
  "temperature": 0,
  "max_tokens_per_call": 512,
  "timeout_seconds": 30
}
```

Replace the example endpoint with the actual connection reference emitted by
the local provider runtime. The launcher rejects HTTP(S), non-loopback gRPC,
and refs containing credentials. Start with `--generation-config <path>`; an
optional `--generation-binding-id <id>` defaults to `data-tools-trial-model`.
Only the dataset-generation Plugin receives the standard binding/config
environment variables. The endpoint, model config, and any provider credentials
are not copied into `runtime.json`; startup configures the binding but does not
make a model call. Startup performs only the local `model.provider.v1` health
check, and fails with a configuration error if that endpoint is not serving the
expected capability.

<!-- Chinese Translation / 中文翻译 -->

# Linux 数据工具试用

此操作员启动器会启动 Catalyst、Echo 及五个真实本地 Plugin 端点：文档解析、知识准备、
数据集生成、数据集准备和 exact-match 评估。两个 Product 共享制品目录，但分别使用独立
SQLite 数据库。默认配置只公开两个 Product API，不启动 Platform daemon。

## 启动 API

启动器要求试用源码根目录包含 `workspace/`、`catalyst/`、`echo/`、`plugins/`，并可选包含
`client/`。需要 Python 3.12 和 `uv`。在试用源码根目录运行：

```bash
python3.12 workspace/packaging/data_tools_trial.py start --source-root "$PWD"
```

默认情况下，Catalyst 使用 `http://127.0.0.1:8014`，Echo 使用
`http://127.0.0.1:8094`。每个地址的 `GET /healthz` 用于就绪检查。Product 路由位于各自基址
下的 `/api/v1/`。通过 Navigator Web Host 访问时，Catalyst 使用 `/api/v1/catalyst/...`，
Echo 使用 `/api/v1/echo/...`；UI 代理会剥离 Product 前缀，再转发到对应 Product 的
`/api/v1/` 路由。启动器会把 Plugin 进程
实际报告的 `direct_plugin_ready` 事件及不透明本地 `connection_ref` 保存到 `runtime.json`；
Product 会通过各自已声明的 connection-ref 环境变量收到这些值。引用字符串来自实际运行的
Plugin 端点，启动器不会伪造引用。

默认运行数据目录为
`${XDG_STATE_HOME:-~/.local/state}/cyrene/data-tools-trial`：

```text
<state-dir>/runtime.json
<state-dir>/artifacts/             # Catalyst 与 Echo 共享
<state-dir>/catalyst/catalyst.sqlite3
<state-dir>/echo/echo.sqlite3
<state-dir>/logs/
```

后续管理操作需使用同一个 `--state-dir`。不同试用实例可用独立状态目录和 API 端口：

```bash
python3.12 workspace/packaging/data_tools_trial.py start \
  --source-root "$PWD" \
  --state-dir "$HOME/.local/state/cyrene/data-tools-trial-secondary" \
  --catalyst-port 8114 --echo-port 8194
```

每个实例拥有自己的数据库和制品目录。除非设置 `--plugin-port-base`，Plugin 端点由操作系统
分配 loopback 端口。

## 可选 Client 和 Navigator UI

UI 配置还会启动当前 Client Control 服务、Vite 开发服务器和规范 Navigator Web Host。它要求
Node.js 24 或更高版本、已安装 Client 依赖（`cd client && npm ci`）以及 Navigator 源码。
默认 Navigator 路径是 `<Cyrene-root>/Cyrene-Services/Cyrene-Navigator`；可用
`--navigator-root` 指定其他 checkout。

```bash
python3.12 workspace/packaging/data_tools_trial.py start \
  --source-root "$PWD" --with-ui
```

UI 默认使用 Client `http://127.0.0.1:5180`、Control
`http://127.0.0.1:5280` 和 Navigator Web Host `http://127.0.0.1:8100`。三个界面均就绪后，
启动器会打印一次性 Navigator 配对码。请在 15 分钟内于 Client 现有配对流程中输入该配对码。
配对码不会写入 `runtime.json` 或日志。Web Host 在服务端拥有 Product API 代理并将 bearer 保存在进程
内存中；Control 进程和浏览器不会收到 `CYRENE_DATA_TOOLS_TOKEN`。UI 是 loopback 开发界面，
不能与 `--allow-remote` 同时使用。

## 远程 API 绑定

默认绑定 loopback，不需要试用 bearer。非 loopback API 绑定要求显式高熵 token（至少 32 个
可打印 ASCII 字节）、`--allow-remote` 和独立配置的 TLS 终止器。通过启动器进程环境提供
token；不要写入参数、`.env` 文件、`runtime.json` 或 shell trace 输出：

```bash
read -rsp "Trial API bearer: " CYRENE_DATA_TOOLS_TOKEN
printf '\n'
export CYRENE_DATA_TOOLS_TOKEN
python3 workspace/packaging/data_tools_trial.py start \
  --source-root "$PWD" --host 0.0.0.0 --allow-remote
unset CYRENE_DATA_TOOLS_TOKEN
```

可选 `CYRENE_DATA_TOOLS_ORGANIZATION_ID` 和 `CYRENE_DATA_TOOLS_WORKSPACE_ID` 用于限定
实例 principal；默认值为 `data-tools-trial` 和 `data-tools`。启动器不会创建或配置 TLS、
防火墙规则、DNS 或公网反向代理。

## 停止、备份和删除数据

停止服务会保留数据库和制品。为保证 SQLite 一致性，应先停止再备份。清理会永久删除已停止
实例的数据，并要求显式确认参数：

```bash
python3.12 workspace/packaging/data_tools_trial.py status --state-dir <state-dir>
python3.12 workspace/packaging/data_tools_trial.py stop --state-dir <state-dir>
python3.12 workspace/packaging/data_tools_trial.py backup --state-dir <state-dir>
python3.12 workspace/packaging/data_tools_trial.py clean \
  --state-dir <state-dir> --confirm-data-loss
```

默认情况下，`backup` 会在状态目录旁的 `data-tools-trial-backups/` 下写入权限为 0600 的
归档；也可用 `--output <path>` 指定另一个尚不存在的路径。归档包含共享制品及两个 Product
数据目录，不包含进程记录或运行时凭据。

## 可选 OCR 与模型生成

PDF 默认使用本地无模型解析路径，DOCX 使用本地处理流程。操作员可另外安装 OCR/模型依赖并
下载 Docling 模型制品，然后在启动器环境中设置
`CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH`。将可选解析依赖安装到当前试用 Plugin 环境，并在
单独的操作步骤中下载模型：

```bash
python -m pip install -e 'plugins/plugins/tools/document-parsing[models]'
docling-tools models download --output-dir <model-directory>
export CYRENE_DOCUMENT_PARSING_ARTIFACTS_PATH=<model-directory>
```

使用默认源码根目录时，如果存在预热环境，请先激活 `<source-root>/.venv`；否则使用
`<state-dir>/venvs/plugins/bin/activate`。修改依赖或模型设置后需重启试用服务。

数据集生成 Plugin 在没有模型提供方时也会启动。手动 SFT 准备仍可用；在操作员配置 provider
之前，请求 QA 生成会明确报告缺少 `model.provider.v1` binding。启动器不会虚构模型端点，也
不会在缺少配置时声称生成能力已就绪。配置 QA 生成前，先启动一个本地 `model.provider.v1`
Plugin 并取得其解析后的本地 gRPC 或 Unix connection ref。JSON 文件遵循 Plugin 配置 schema：

```json
{
  "model_endpoint": "grpc://127.0.0.1:19080",
  "model": "scripted-or-local-model",
  "temperature": 0,
  "max_tokens_per_call": 512,
  "timeout_seconds": 30
}
```

将示例 endpoint 替换为本地 Provider runtime 实际报告的 connection ref。启动器会拒绝 HTTP(S)、
非 loopback gRPC 以及含凭据的 ref。使用 `--generation-config <path>` 启动；可选
`--generation-binding-id <id>` 默认是 `data-tools-trial-model`。只有 dataset-generation Plugin
会收到标准 binding/config 环境变量。Endpoint、模型配置和任何 Provider 凭据都不会复制到
`runtime.json`；启动时只检查本地 Provider 的 `model.provider.v1` health，不调用模型；如果端点未就绪，
启动器会报告配置错误。
