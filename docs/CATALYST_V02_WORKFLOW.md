# Catalyst v0.2 Source Review and Publishing Trial

This guide runs the Catalyst-owned v0.2 workflow against a fixed synthetic
business corpus. It covers real file parsing, OCR diagnostics, human review,
one bounded model-assisted QA draft, and the existing knowledge/SFT publishing
authority. The fixed corpus is test material; it contains no customer or
personal data.

本文使用固定的合成业务语料运行 Catalyst v0.2 工作流，覆盖真实文件解析、OCR 诊断、人工
审核、一次有预算限制的模型辅助 QA 草稿，以及现有知识包/SFT 发布权威。固定语料仅用于测试，
不含客户或个人数据。

## Runtime and local prerequisites

Run from the task root, where `workspace/`, `catalyst/`, `plugins/`, and the
optional `client/` source tree are siblings. Python 3.12 and `uv` are required;
the launcher uses the task-root `.venv` when present. Without it, the launcher
installs the pinned Plugin and Product environments below the selected
`--state-dir/venvs/` directory. The API-only runtime does not require Node.js.
The optional Client UI needs Node.js 24 or newer, installed Client `node_modules`,
and the canonical Navigator Web Host source tree.

从任务根目录启动；`workspace/`、`catalyst/`、`plugins/` 和可选的 `client/` 源码目录应为同级。
需要 Python 3.12 与 `uv`。启动器优先复用任务根 `.venv`；否则会在指定的
`--state-dir/venvs/` 下安装锁定的 Plugin 和 Product 环境。纯 API 运行不需要 Node.js；可选的
Client UI 需要 Node.js 24 或更新版本、已安装的 Client `node_modules` 以及规范 Navigator Web
Host 源码。

The default API listener is loopback-only. Catalyst serves at
`http://127.0.0.1:8024`; its readiness endpoint is `/healthz`. Shared files and
Plugin staging live under the state directory's `artifacts/`, while Catalyst's
SQLite database stays in `catalyst/catalyst.sqlite3`. `--catalyst-only` starts
only Catalyst plus document parsing, knowledge preparation, dataset generation,
and dataset preparation. It does not start Echo or the evaluation Plugin. The
flag is opt-in; leaving it out preserves the existing Catalyst/Echo trial.

默认 API 仅绑定 loopback：Catalyst 地址为 `http://127.0.0.1:8024`，就绪检查为 `/healthz`。共享
文件和 Plugin staging 位于 state directory 的 `artifacts/`；Catalyst SQLite 数据库独立位于
`catalyst/catalyst.sqlite3`。`--catalyst-only` 仅启动 Catalyst 及文档解析、知识准备、数据集生成、
数据集准备四个 Plugin，不启动 Echo 或 evaluation Plugin。该选项默认关闭；不指定时保留原有
Catalyst/Echo 试用模式。

The launcher persists a secret-free `runtime.json` and separate logs in the
state directory. The default state directory is
`$XDG_STATE_HOME/cyrene/catalyst-v02-20261007`, or
`~/.local/state/cyrene/catalyst-v02-20261007` when `XDG_STATE_HOME` is unset.
The shared artifact root and Catalyst database are also shown by `status`.

启动器会在 state directory 保存不含密钥的 `runtime.json` 和独立日志。默认 state directory 为
`$XDG_STATE_HOME/cyrene/catalyst-v02-20261007`；若未设置 `XDG_STATE_HOME`，则使用
`~/.local/state/cyrene/catalyst-v02-20261007`。`status` 也会显示共享制品目录和 Catalyst 数据库
路径。

## Start Catalyst and the optional UI

The actual model connector must already expose a local `model.provider.v1`
DirectPluginRuntime endpoint. Put only the resolved local connection reference,
model alias, temperature, and bounds in the generation JSON file; never put an
API token or provider credential there. For example:

实际模型 connector 必须先启动并提供本机 `model.provider.v1`
DirectPluginRuntime endpoint。generation JSON 只放已解析的本机连接引用、模型别名、temperature
和预算；不要放 API token 或 provider 凭据。例如：

```json
{
  "model_endpoint": "grpc://127.0.0.1:36875",
  "model": "qwen2.5-1.5b-instruct-local",
  "temperature": 0,
  "max_tokens_per_call": 512,
  "timeout_seconds": 180
}
```

The reference must be returned by the local Plugin runtime. HTTP URLs, remote
gRPC hosts, and credential-bearing references are rejected. Catalyst's
`--generation-binding-id` must match the existing model binding. A configured
endpoint enables `generateQa`; without one, manual source review and SFT
preparation remain available while model generation returns an explicit
unavailable result.

连接引用必须由本地 Plugin runtime 返回；HTTP URL、远程 gRPC 主机和包含凭据的引用都会被拒绝。
Catalyst 的 `--generation-binding-id` 必须与现有模型 binding 一致。配置 endpoint 后可启用
`generateQa`；未配置时仍可人工审核来源和准备 SFT，模型生成会明确返回不可用。

Start API-only:

纯 API 启动：

```bash
python3.12 workspace/packaging/data_tools_trial.py start \
  --source-root "$PWD" \
  --state-dir "$HOME/.local/state/cyrene/catalyst-v02-20261007" \
  --catalyst-only \
  --catalyst-port 8024 \
  --generation-config "$PWD/generation-config.json" \
  --generation-binding-id "<resolved-model-binding-id>"
```

To assemble the current Client UI, add the canonical Navigator source root and
loopback ports. The Web Host sends any trial bearer to Catalyst server-side;
the browser does not hold `CYRENE_DATA_TOOLS_TOKEN`.

如需组装当前 Client UI，增加规范 Navigator 源码目录和 loopback 端口。Web Host 在服务端向
Catalyst 注入 trial bearer；浏览器不会持有 `CYRENE_DATA_TOOLS_TOKEN`。

```bash
PATH="/path/to/node-24/bin:$PATH" \
python3.12 workspace/packaging/data_tools_trial.py start \
  --source-root "$PWD" \
  --state-dir "$HOME/.local/state/cyrene/catalyst-v02-20261007" \
  --catalyst-only --catalyst-port 8024 \
  --with-ui \
  --navigator-root "/path/to/Cyrene-Navigator" \
  --navigator-port 8101 --client-port 5181 --control-port 5281 \
  --generation-config "$PWD/generation-config.json" \
  --generation-binding-id "<resolved-model-binding-id>"
```

Startup prints the UI URLs and a one-time Navigator pairing code. Keep the code
private and enter it only in the local Navigator pairing screen. Inspect and
manage the runtime with:

启动后会打印 UI 地址和一次性 Navigator pairing code。妥善保管该 code，只在本机 Navigator 配对
页面输入。使用以下命令查看和管理 runtime：

```bash
python3.12 workspace/packaging/data_tools_trial.py status --state-dir "$STATE_DIR"
python3.12 workspace/packaging/data_tools_trial.py stop --state-dir "$STATE_DIR"
python3.12 workspace/packaging/data_tools_trial.py backup --state-dir "$STATE_DIR"
python3.12 workspace/packaging/data_tools_trial.py clean \
  --state-dir "$STATE_DIR" --confirm-data-loss
```

`stop` preserves databases and artifacts. `backup` writes an owner-only
archive. `clean` permanently removes the stopped instance and requires an
explicit data-loss flag.

`stop` 会保留数据库和制品。`backup` 会创建仅所有者可读的归档。`clean` 会永久删除已停止实例，
必须显式提供数据丢失确认选项。

## OCR and parser configuration

The default OCR root is
`<source-root>/reports/ocr-runtime/extracted`. The launcher points the parser
directly at that tree's `usr/bin/tesseract` executable and
`usr/share/tesseract-ocr/5/tessdata`, and prepends its extracted library path to
`LD_LIBRARY_PATH`. It does not fall back to a system Tesseract binary and does
not download language data or OCR models. Default settings are engine `auto`,
languages `eng,chi_sim`, 200 DPI, and minimum confidence 0.8.

默认 OCR 根目录为 `<source-root>/reports/ocr-runtime/extracted`。启动器会直接指定该目录中的
`usr/bin/tesseract`、`usr/share/tesseract-ocr/5/tessdata`，并把提取出的动态库目录加到
`LD_LIBRARY_PATH` 前面。它不会回退到系统 Tesseract，也不会下载语言数据或 OCR 模型。默认设置为
engine `auto`、语言 `eng,chi_sim`、200 DPI、minimum confidence 0.8。

Override the OCR inputs with `--ocr-runtime-root`, `--ocr-engine`,
`--ocr-languages`, `--ocr-dpi`, and `--ocr-minimum-confidence`. The selected
runtime root must use the same extracted directory layout. If the executable,
shared libraries, or requested traineddata are missing, parse reports retain
page-scoped warnings such as `ocr.engine_unavailable` or
`ocr.language_data_unavailable`; the launcher does not silently substitute a
different system installation. Use `--ocr-engine none` to disable OCR
intentionally. `--docling-artifacts-path` is a separate optional directory for
preloaded Docling layout models and is never treated as Tesseract data.

可通过 `--ocr-runtime-root`、`--ocr-engine`、`--ocr-languages`、`--ocr-dpi` 和
`--ocr-minimum-confidence` 覆盖 OCR 设置；自定义 root 必须保持相同的提取目录布局。如果缺少 OCR
可执行文件、动态库或请求的 traineddata，parse report 会保留页级 warning，例如
`ocr.engine_unavailable` 或 `ocr.language_data_unavailable`；启动器不会静默替换成系统安装。使用
`--ocr-engine none` 可显式关闭 OCR。`--docling-artifacts-path` 是可选的 Docling layout model
预加载目录，与 Tesseract 数据分开。

## Remote binding

Keep development instances on loopback. A remote Catalyst listener requires
`CYRENE_DATA_TOOLS_TOKEN` in the launcher environment, with at least 32
printable ASCII bytes, and both `--host <non-loopback-address>` and
`--allow-remote`. Do not put the token in the command line, generation JSON,
runtime record, or browser settings. Terminate TLS at a separately managed
reverse proxy. The development Client/Web Host mode is loopback-only and cannot
be combined with a remote bind.

开发实例应保持 loopback。远程 Catalyst listener 必须在启动器环境变量中设置
`CYRENE_DATA_TOOLS_TOKEN`（至少 32 个可打印 ASCII 字节），同时提供 `--host <非 loopback 地址>` 和
`--allow-remote`。不要把 token 放入命令行、generation JSON、runtime record 或浏览器设置。TLS 应由
单独管理的 reverse proxy 终止。开发 Client/Web Host 模式只绑定 loopback，不能与远程监听组合。

## Run the v0.2 acceptance workflow

First verify the immutable synthetic corpus without making API or model calls:

先验证固定合成语料；该步骤不会调用 API 或模型：

```bash
python3.12 workspace/scripts/verify-catalyst-v02.py --fixtures-only
```

For live acceptance, start Catalyst in catalyst-only mode and start the
existing local model provider plus its `model-api-connector` DirectPluginRuntime.
The default task-local identity checks expect binding
`catalyst-v02-local-qwen`, model `qwen2.5-1.5b-instruct-local`, and cached model
revision `989aa7980e4cf806f80c7fef2b1adb7bc71aa306`. The verifier requires the
connector readiness record, local provider readiness record, and an empty
provider usage ledger before it starts. It never sends a direct completion
probe.

进行在线验收前，以 catalyst-only 模式启动 Catalyst，再启动既有本机 model provider 和它的
`model-api-connector` DirectPluginRuntime。默认身份校验期望 binding
`catalyst-v02-local-qwen`、模型 `qwen2.5-1.5b-instruct-local`、缓存 revision
`989aa7980e4cf806f80c7fef2b1adb7bc71aa306`。验收器要求 connector readiness、local provider
readiness 和空 usage ledger 均匹配后才继续；它不会直接发送 completion probe。

```bash
python3.12 workspace/scripts/verify-catalyst-v02.py \
  --catalyst-url http://127.0.0.1:8024 \
  --source-root "$PWD" \
  --state-dir "$HOME/.local/state/cyrene/catalyst-v02-20261007" \
  --generation-config "$PWD/generation-config.json" \
  --generation-binding-id catalyst-v02-local-qwen \
  --expected-model qwen2.5-1.5b-instruct-local \
  --expected-model-revision 989aa7980e4cf806f80c7fef2b1adb7bc71aa306 \
  --provider-runtime "$PWD/reports/model-api-connector-runtime.json" \
  --provider-readiness "$PWD/reports/local-provider-readiness.json" \
  --provider-usage-ledger "$PWD/reports/local-provider-usage.jsonl" \
  --navigator-root "/path/to/Cyrene-Navigator"
```

The live verifier uploads the 14 manifest-pinned native files in one repeated
`files[]` batch; starts one parse run; reads one durable report per file; checks
native text, located OCR output, Office structure, malformed input, and
unsupported input; and proves open parser/OCR review issues block approval
until they are acknowledged. For this synthetic trial only, it explicitly
enables Knowledge policy on every block from a successful parse source. It keeps
all 24 JSONL conversation blocks out of model context until after generation,
and initially permits model context from only one rich original prose block
(this corpus selects `handoff.md` because its TXT counterpart has no sufficiently rich block).
The operator-only metadata remains outside learned
text.

在线验收器会通过重复 `files[]` multipart 字段一次上传 manifest 固定的 14 个原生文件；启动一个
parse run；读取每个文件的持久化报告；检查原生文本、带定位信息的 OCR 输出、Office 结构、损坏
输入和不支持输入；并验证未解决的 parser/OCR issue 会阻断审批，直到人工确认。仅在这份合成试用
数据上，所有成功解析来源的 block 都会显式启用 Knowledge 策略。24 个 JSONL 对话 block 在模型
生成前保持不可训练；QA 生成时只允许一个原始富内容 prose block（当前固定语料选择 `handoff.md`）作为上下文。
仅操作员使用的元数据不会进入学习文本。

The verifier places an exclusive marker before issuing exactly one `generateQa`
run with `maxExamples=1`, `maxCalls=1`, and `maxOutputTokens=512`. It checks the
generated receipt against the configured binding/model and selected original
prose block, checks the provider's single completed usage-ledger row and token
count, and proves the unapproved draft cannot publish. The acceptance operator
records explicit reviewer approvals through the human-review API; the verifier
preserves the generated receipt while projecting its QA into the approved
conversation format, then enables training on the 24 manually reviewed JSONL
records in a new immutable revision. The Knowledge ZIP must cover every
successful parse source, including readable PPTX speaker notes and XLSX formula
cache evidence. Empty Knowledge blocks are skipped and counted in the plugin's
conversion report. The SFT ZIP must contain the 12 manual source families and the
generated prose family. Before approval, the verifier restarts the launcher-owned
runtime with the generated revision still in DRAFT, compares its generation
receipt, blocks, parse reports, and review queue, and confirms publication still
returns HTTP 409. It then prepares both profiles, publishes a DatasetVersion,
restarts again, and compares parse reports, review queue, blocks, package bytes,
and published version snapshots. The result
and sanitized evidence are written to
`reports/catalyst-v02-acceptance/acceptance.json` with mode 0600. A failure
after the generation marker is created must be investigated; rerunning cannot
issue a second model call from that evidence directory.

验收器会先创建独占 marker，再只提交一次 `generateQa`，预算为 `maxExamples=1`、`maxCalls=1`、
`maxOutputTokens=512`。它会校验生成回执绑定的真实原始 prose block、provider usage ledger 中唯一一条
完成记录及 token 数；验证未审批草稿不能发布。Acceptance operator 会通过 human-review API 明确
记录 reviewer approval；验收器保留生成回执，将 QA 转换为已审核的 conversation 格式，再在新的不可变
修订上为 24 条 JSONL 手工审核记录启用训练。Knowledge ZIP 必须覆盖每个成功解析来源，并可读回 PPTX
演讲者备注和 XLSX 公式缓存证据；Knowledge 会跳过空 block 并在 conversion report 中计数；SFT ZIP 必须包含
12 个手工 source family 和生成 prose family。生成草稿后、审批前，验收器会重启启动器管理的 runtime，比较
generation receipt、blocks、parse reports 和 review queue，并确认未审批内容仍以 HTTP 409 阻止发布。
之后验收器准备两个 profile、发布 DatasetVersion、再次重启 runtime，并比较 parse reports、review
queue、blocks、制品 bytes 和已发布版本快照。脱敏证据写入
`reports/catalyst-v02-acceptance/acceptance.json`，权限为 0600。创建 generation marker 后若验收失败，
必须先调查；相同 evidence directory 会阻止再次发起模型调用。

The model provider used here is the explicitly identified local Qwen model. Before
generation, the verifier reads only its loopback `/healthz` endpoint and requires
the pinned model revision, a 0/1 call budget, and a 512-token output cap. A
synthetic/mock/scripted provider is not accepted as real-provider evidence. The
human-review API calls are acceptance-operator actions; they do not claim that
the end user personally reviewed or approved the synthetic records.
`--skip-restart` is available for debugging; it records `PARTIAL` and exits with
status 2, so it cannot be mistaken for complete restart-recovery acceptance.

If a live verifier stops after a persisted `generateQa` run fails with
`INVALID_REQUEST` before the model provider accepts a completion, recovery is
limited to the same dataset and evidence directory. Confirm that the provider
usage ledger is empty and its loopback health reports `calls_started=0`, then
rerun with `--resume-dataset-id <existing-dataset-id>`. The verifier preserves
the original marker and failed run, writes an exclusive resume marker, and
admits at most one retry. Provider usage or an existing resume marker blocks
recovery. Never use this path after a provider call has started.

本流程使用身份明确的本地 Qwen 模型。synthetic/mock/scripted provider 不能作为真实模型证据。
生成前，验收器只读取 loopback `/healthz`，并要求模型 revision 固定、调用预算为 0/1、输出上限为 512 tokens。
其中 human-review API 操作由 acceptance operator 执行，不表示终端用户本人查看或批准了这些合成记录。
`--skip-restart` 仅供调试；结果会标记为 `PARTIAL` 并以状态码 2 退出，不能误认为完整的重启恢复验收。

如果验收器在 provider 尚未接受 completion 前，因为已持久化的 `generateQa` `INVALID_REQUEST` 失败而停止，
只能在同一 dataset 和 evidence directory 上恢复。先确认 usage ledger 为空且 loopback health 的
`calls_started=0`，再使用 `--resume-dataset-id <现有 dataset ID>`。验收器保留原 marker 和失败 run，
创建独占 resume marker，最多重新提交一次；出现 provider 使用记录或已有 resume marker 时都会拒绝恢复。
Provider 已开始调用后不能使用此恢复路径。

## Test and lint

Run the focused deterministic tests from the task root:

在任务根目录运行定向、确定性的测试：

```bash
.venv/bin/pytest -q \
  workspace/tests/test_catalyst_v02_fixtures.py \
  workspace/tests/test_catalyst_v02_launcher.py \
  workspace/tests/test_catalyst_v02_acceptance.py
.venv/bin/ruff check \
  workspace/packaging/data_tools_trial.py \
  workspace/packaging/data_tools_web_host.py \
  workspace/scripts/verify-catalyst-v02.py \
  workspace/tests/test_catalyst_v02_launcher.py \
  workspace/tests/test_catalyst_v02_acceptance.py
```
