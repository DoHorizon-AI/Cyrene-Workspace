# Cyrene Navigator V8 connection handoff / Cyrene Navigator V8 连接交接

> Historical package snapshot / 历史交付快照：本目录只描述当时已固定的 V8 Alpha
> 安装包，不是当前开发架构或依赖权威。当前 Platform–Plugins 边界以
> `docs/platform-plugin-direct-boundary-remediation-2026-09-08.md` 为准；新代码不得恢复
> 本快照中的 Platform CES 业务代理路径。

This is an operator note for the Windows Alpha package. It describes how the
installed desktop client connects to already deployed Cyrene services. The
final package handoff includes `release-lock.json`, `SOURCE_VERSIONS.md`,
and `WINDOWS_ACCEPTANCE.md` beside the installer; those release documents are
the source of truth for package and dependency pins.

这是 Windows Alpha 安装包的操作说明，说明已安装桌面客户端如何连接已经部署的 Cyrene
服务。最终 package handoff 在 installer 旁提供 `release-lock.json`、`SOURCE_VERSIONS.md`
和 `WINDOWS_ACCEPTANCE.md`；这些 release 文档才是安装包和依赖 pin 的权威来源。

The package contains the Tauri shell, the pinned DeepSeek Harness/Profile,
bundled Node runtime, and approved native tools. It does **not** contain or
start Exchange, Cyrene persistence, Workspace/Identity, Reactor, or an external
model service.

安装包包含 Tauri 壳、固定版本的 DeepSeek Harness/Profile、随包 Node runtime 和 approved
native tools；它**不包含也不会启动** Exchange、Cyrene persistence、Workspace/Identity、
Reactor 或外部模型服务。

```text
Navigator package
  └─ Tauri → pinned Harness/Profile → dynamic loopback WebView
       ├─ Exchange URL/token → Exchange → Reactor endpoint or external provider
       └─ Persistence URL/token + Workspace/device → Cyrene persistence service
```

The default `cyrene-navigator` profile routes model requests through the
Exchange provider. The native host and `cy-manifest` are resolved from the
installed resource tree by the shell; they are not user-selected server
paths.

默认 `cyrene-navigator` profile 通过 Exchange provider 路由模型请求。native host 和
`cy-manifest` 由桌面壳从安装包 resource tree 中解析，不是用户手工选择的服务器路径。

## Files and reading order / 文件与阅读顺序

| File | Use / 用途 |
| --- | --- |
| `installers/Cyrene-Navigator-V8-x64-setup.exe` | Accepted NSIS installer; keep its default per-user destination / 已验收的 NSIS 安装包，保留默认按用户安装位置 |
| `SOURCE_VERSIONS.md`, `release-lock.json`, `SHA256SUMS` | Exact source, dependency, package and delivery checksums / 精确源码、依赖、安装包与交付校验和 |
| [connection.example.json](connection.example.json), [Connect-Navigator.ps1](Connect-Navigator.ps1) | Copy non-sensitive settings, then validate and launch / 复制非敏感配置，再校验与启动 |
| [BACKENDS.md](BACKENDS.md) | Service administrator's startup and version-composition notes / 服务管理员的启动与版本组合说明 |
| [WINDOWS_ACCEPTANCE.md](WINDOWS_ACCEPTANCE.md) | Accepted scenarios and known limits / 已验收场景与已知限制 |

Read the connection steps below first. Administrators use `BACKENDS.md` to
prepare the independent services. Use `SOURCE_VERSIONS.md` to verify the exact
installer before installing it; the archive carries the accepted NSIS path.

先阅读下方连接步骤；管理员按 `BACKENDS.md` 准备独立后端。安装前按 `SOURCE_VERSIONS.md`
核对安装包哈希；本交付只携带已验收的 NSIS 安装路径。

## Install and connect / 安装与连接

1. Obtain the exact V8 Windows installer and its companion release documents
   (`release-lock.json`, `SOURCE_VERSIONS.md`, and `WINDOWS_ACCEPTANCE.md`) from
   the private Alpha handoff. The package is unsigned and intended for private
   proof use; review the normal Windows warning according to your organization’s
   policy. Install it through the Windows installer, then use the connector
   below to launch the installed executable. Do not use a source checkout or
   development runtime for this flow.

   从私有 Alpha 交接处取得精确的 V8 Windows installer 及其 companion release 文档
   （`release-lock.json`、`SOURCE_VERSIONS.md` 和 `WINDOWS_ACCEPTANCE.md`）。该包未签名，
   只用于私有 Alpha proof；按组织策略处理 Windows 的普通安全提示。通过 Windows installer
   安装，然后使用下面的 connector 启动已安装 executable；这条流程不需要 source checkout
   或开发 runtime。

2. Use the connector delivered beside this README. It reads the non-sensitive
   fields from `connection.json`, verifies the default installed executable,
   and asks for the Exchange and persistence tokens with hidden input on every
   real launch. The tokens are placed in the child process environment only;
   they are never written to a file, command line, proof record, or WebView.
   The desktop shell fails closed when a required setting is absent.

   使用本 README 同目录交付的 connector。它从 `connection.json` 读取非敏感字段，校验默认
   安装程序，并在每次实际启动时以隐藏输入询问 Exchange 和 persistence token。token 只会
   放入子进程的 environment，不会写入文件、命令行、proof record 或 WebView。缺少必需配置
   时，桌面壳会 fail closed。

   | Setting | Purpose / 用途 |
   | --- | --- |
   | `CYRENE_EXCHANGE_URL` | Exchange provider API base URL / Exchange provider API 基础地址 |
   | `CYRENE_EXCHANGE_TOKEN` | Secret used for Exchange authentication / Exchange 认证 secret |
   | `CYRENE_HARNESS_MODEL` | Model ID registered and routable by Exchange / Exchange 中注册并可路由的模型 ID |
   | `CYRENE_PERSISTENCE_URL` | Cyrene Session persistence service base URL / Cyrene Session persistence 服务地址 |
   | `CYRENE_SESSION_TOKEN` | Secret used for persistence access / persistence 访问 secret |
   | `CYRENE_WORKSPACE_ID` | Workspace scope used by persistence and recovery / persistence 与恢复使用的 Workspace 范围 |
   | `CYRENE_ACCOUNT_PROFILE_ID` | Stable local account-profile namespace for retained input / retained input 使用的稳定本地账户 profile 命名空间 |
   | `CYRENE_DEVICE_ID` *(optional)* | Stable unique client identity for writer fencing; defaults to `navigator` / writer fencing 使用的稳定唯一客户端标识，默认 `navigator` |
   | `CYRENE_HARNESS_CONTEXT_WINDOW`, `CYRENE_HARNESS_MAX_TOKENS` *(optional)* | Model request limits when the profile needs an override / profile 需要覆盖时的模型请求限制 |

   `CYRENE_ACCOUNT_PROFILE_ID` and `CYRENE_DEVICE_ID` identify local/client
   namespaces; neither grants Workspace permission. Authorization comes from
   the account and service credentials. Give every device a stable distinct
   device ID when several clients may read or take over one Session.

   `CYRENE_ACCOUNT_PROFILE_ID` 和 `CYRENE_DEVICE_ID` 只标识本地或客户端命名空间，均不授予
   Workspace 权限；权限来自账户和服务凭据。多个客户端可能读取或接管同一 Session 时，
   每台设备应使用稳定且互不相同的 device ID。

   The minimum connection prerequisites are:

   - Exchange service administrator: Exchange URL, bearer token, and routable model ID.
   - Cyrene persistence service administrator: Persistence URL, bearer token, and Workspace ID.
   - This client operator: a stable local account-profile ID and a distinct device ID.

   The installed desktop alone does not provide either backend.

   The connector maps these environment names from the JSON keys in
   [`connection.example.json`](connection.example.json). Copy the example to
   `connection.json`, replace only its non-sensitive placeholders, and run:

   On Windows PowerShell 5.1, use the process-scoped execution-policy form
   below. `Bypass` applies only to the child PowerShell process that runs this
   script; it does not change the User or LocalMachine policy.

   在 Windows PowerShell 5.1 中，使用下面的仅限进程执行策略形式。`Bypass` 只作用于运行该
   脚本的 PowerShell 子进程，不会修改 User 或 LocalMachine policy。

   ```powershell
   # Run these commands from the directory containing this README and script.
   Copy-Item .\connection.example.json .\connection.json
   # Edit .\connection.json with the service URLs, IDs, device, and model.
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Connect-Navigator.ps1 -ConfigPath .\connection.json -ValidateOnly
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Connect-Navigator.ps1 -ConfigPath .\connection.json
   ```

   `-ValidateOnly` checks the JSON, URL policy, default installed path, and V8
   executable digest without asking for tokens or starting Navigator. A real
   launch prompts for both tokens and returns after `Start-Process`; the child
   continues with its inherited Process-scoped settings. This handoff has no
   GUI connection editor yet, so changing a connection means editing the
   non-sensitive JSON and launching the connector again. The connector does
   not start Exchange, persistence, or any other backend.

   最小连接前提如下：

   - Exchange 服务管理员：Exchange URL、bearer token 和可路由的 model ID。
   - Cyrene persistence 服务管理员：Persistence URL、bearer token 和 Workspace ID。
   - 本机操作员：稳定的 account-profile ID，以及与其他设备不同的 device ID。

   已安装的桌面程序本身不提供这两个 backend。

   connector 会从 JSON key 映射这些 environment name。先将
   [`connection.example.json`](connection.example.json) 复制为 `connection.json`，只修改其中
   的非敏感 placeholder，然后运行上面的 PowerShell 命令。`-ValidateOnly` 只检查 JSON、URL
   策略、默认安装路径和 V8 executable digest，不询问 token，也不启动 Navigator。实际启动
   会询问两个 token，并在 `Start-Process` 返回后结束 connector；子进程继续使用继承的
   Process 环境。本交付暂时没有 GUI connection editor，修改连接需要编辑非敏感 JSON 后重新
   启动 connector。connector 不会启动 Exchange、persistence 或任何其他 backend。

3. Confirm that the configured Exchange and persistence services are reachable
   from the machine and that the Workspace identity is authorized in both
   places. For remote services use HTTPS and the organization’s trusted
   certificate chain. The packaged Harness is started with system CA
   verification; do not disable certificate validation. The local Harness page
   uses a system-assigned `127.0.0.1` port and is not a public service endpoint.

   确认配置的 Exchange 与 persistence 服务可从本机访问，并且 Workspace identity 在两边都
   已授权。远程服务使用 HTTPS 和组织信任的证书链。随包 Harness 使用系统 CA 校验，不能
   关闭证书验证。本地 Harness 页面使用系统分配的 `127.0.0.1` 端口，不是公开服务地址。

4. If the startup page shows `Configuration is required` with missing setting
   names, edit the JSON and run the connector again. A runtime failure shows
   `Navigator could not start`. The sanitized runtime status is stored in the
   application data directory under `%LOCALAPPDATA%\Cyrene\Navigator`; it
   contains state, port and a generic message, not the ready URL or credentials.

   如果启动页显示 `Configuration is required` 和缺失项名称，修改 JSON 后重新运行 connector。
   runtime 启动失败则显示 `Navigator could not start`。脱敏 runtime status 位于
   `%LOCALAPPDATA%\Cyrene\Navigator` 下的应用数据目录，只包含 state、port 和通用消息，
   不包含 ready URL 或 credential。

5. When the Harness page is ready, send one ordinary text message and verify
   that the selected model is available through Exchange. A successful chat
   does not prove Reactor deployment; Exchange may route to a Reactor endpoint
   or to a separately configured external provider.

   Harness 页面 ready 后，发送一条普通文本消息，确认所选模型可经 Exchange 使用。聊天成功
   不等于 Reactor 部署已经验收；Exchange 可以把请求路由到 Reactor Endpoint，也可以路由到
   另行配置的外部 provider。

## Exchange and replaceable model endpoints / Exchange 与可替换模型端点

Exchange owns model authentication, routing, protocol adaptation, usage and
request audit for the default Navigator path. Navigator only needs the
Exchange URL, its token, and the model ID. Reactor remains the Cyrene-managed
serving path; an OpenAI-compatible external endpoint may be connected behind
Exchange without changing Navigator Session persistence.

默认 Navigator 路径中，Exchange 负责模型认证、路由、协议适配、usage 和请求审计。Navigator
只需要 Exchange URL、Exchange token 和模型 ID。Reactor 仍是 Cyrene 管理的 serving 路径；
也可以在 Exchange 后接入 OpenAI-compatible 外部端点，而不改变 Navigator 的 Session 持久化。

The upstream Harness provider settings also allow a user profile to add a
direct external provider. That is an independent route and is outside the
adoption proof’s Exchange path; Exchange usage/audit behavior must not be
assumed for direct-provider requests. Keep those credentials in the provider’s
own supported account storage; the connector only handles the two Cyrene service
tokens described above.

上游 Harness provider settings 也允许用户 profile 添加直接外部 provider。这是独立入口，
不属于 adoption proof 的 Exchange 路径；直接 provider 请求不能假定具有 Exchange 的
usage/audit 行为。相关 credential 必须保存在 provider 自己支持的 account storage 中。

## Import and Session recovery / 导入与 Session 恢复

The current desktop import action is **File → Import Codex Session**:

当前桌面导入入口是 **File → Import Codex Session**：

1. Select a Codex JSONL archive and review the read-only preview and conversion
   report.
2. Choose an absolute local working directory and, if needed, the
   `cyrene-navigator` agent preset.
3. Click Continue to create a new live Session. Imported Tool Calls, Tool
   Results and approvals are archive records only; they are never executed.

1. 选择 Codex JSONL archive，查看只读预览和 conversion report。
2. 选择本机绝对 working directory，并按需选择 `cyrene-navigator` agent preset。
3. 点击 Continue 创建新的 live Session。导入的 Tool Call、Tool Result 和 approval 只作为
   archive record 保存，绝不会执行。

To recover an existing Session, open **Recover Session**, refresh the
authorized list, select a Session and inspect its read-only ownership state.
Use **Continue here** when this device can continue without taking ownership.
Use **Take over writing here** only as an explicit user action after reviewing
the observed epoch. The old writer is fenced and receives a conflict; there is
no automatic takeover. If this runtime still holds a stale local Session after
losing ownership, close and reopen Navigator before taking over.

恢复已有 Session 时，打开 **Recover Session**，刷新已授权列表，选择 Session 并查看只读
ownership 状态。如果本机可以在不取得 ownership 的情况下继续，使用 **Continue here**。
只有在确认 observed epoch 后，才通过明确的用户操作使用 **Take over writing here**。旧 writer
会被 fencing 并收到 conflict，不会自动 takeover。如果当前 runtime 丢失 ownership 后仍持有
旧的本地 Session，先关闭并重新打开 Navigator，再执行 takeover。

Recovery requires the recorded working directory to exist on the current
device. If it does not, use Import or Continue with a selected local workspace;
Linux paths are not silently reused on Windows.

恢复要求记录的 working directory 在当前设备上存在。如果不存在，请使用 Import，或在 Continue
时选择本机 workspace；Linux 路径不会在 Windows 上静默复用。

## Common failures / 常见失败

| Symptom | Action / 处理 |
| --- | --- |
| `Configuration is required` / `configuration_required` | Edit the non-sensitive JSON and run `Connect-Navigator.ps1` again; there is no local default backend. / 修改非敏感 JSON 后重新运行 `Connect-Navigator.ps1`，不存在本地默认后端。 |
| HTTP 401/403 from Exchange or persistence | Check the matching token, Workspace authorization and trusted origin policy. Do not create another Session to bypass the rejection. / 检查对应 token、Workspace 授权和 trusted origin policy，不要创建另一个 Session 来绕过拒绝。 |
| TLS or certificate failure | Use the correct HTTPS service URL and install the organization-approved CA through normal OS administration. Do not disable system CA validation. / 使用正确 HTTPS 地址，并通过正常 OS 管理安装组织批准的 CA，不要关闭系统 CA 校验。 |
| Exchange timeout or network disconnect | Review the Session and service status. The client may retain an unconfirmed local copy; only a durable receipt confirms delivery. Restore/Retry/Send is explicit and never automatically resubmits a model request. / 检查 Session 和服务状态。客户端可能保留未确认的本地副本，只有 durable receipt 才确认已交付；Restore/Retry/Send 都必须显式操作，不会自动重发模型请求。 |
| Ownership changed / `SESSION_ALREADY_LOCAL` | Refresh and re-observe. If a stale local runtime remains, close and reopen before an explicit takeover. / 刷新并重新 observe；若本地仍有 stale runtime，先关闭重开再显式 takeover。 |
| Recorded workspace unavailable | Select an available local directory through Import or Continue. / 通过 Import 或 Continue 选择可用的本机目录。 |
| Native Tool or runtime failed | Keep the installed resource tree intact and reinstall the exact package if resources are missing. Do not point native paths at a development checkout. / 保持安装包 resource tree 完整，资源缺失时重新安装精确 package，不要把 native path 指向开发 checkout。 |
| Runtime stopped or failed | Follow the visible generic notice and close/reopen Navigator. Historical Tool records are not replayed. / 按页面通用提示关闭并重新打开 Navigator，历史 Tool record 不会重放。 |

## Current Alpha limits / 当前 Alpha 限制

- Exchange and Cyrene persistence are separately deployed services. The
  installer does not provide a production server, database, GPU scheduler or
  Workspace/SSO/RBAC onboarding flow.
- Exchange 与 Cyrene persistence 是独立部署的服务。安装包不提供生产 server、database、
  GPU scheduler，也不提供 Workspace/SSO/RBAC onboarding 流程。
- The proof services and loopback addresses used during adoption testing are
  not a managed production deployment. Configure real service URLs and enter
  the two service tokens through `Connect-Navigator.ps1`; the script does not
  provision or manage those services.
- adoption proof 使用的 fixture service 和 loopback 地址不是 managed production deployment；
  使用 `Connect-Navigator.ps1` 配置真实服务地址并输入两个 service token，脚本不会 provision
  或管理这些服务。
- The Linux NVIDIA/CUDA training and Reactor serving vertical is a separate
  Platform/Yield/Reactor workstream and is not shipped by this Windows client.
- Linux NVIDIA/CUDA training 和 Reactor serving vertical 属于独立的 Platform/Yield/Reactor
  工作流，不随本 Windows client 提供。
- Hosted Navigator CI currently has a billing/spending-limit environment
  blocker. This handoff note does not infer a green CI result or a completed
  Phase 0 gate.
- 当前 hosted Navigator CI 仍有 billing/spending-limit 环境阻塞。本交接说明不推导 CI 全绿，
  也不表示 Phase 0 总门已完成。

For the authoritative acceptance state, use the Workspace lifecycle plan and
the Phase 0 evidence record; this README is an operational connection guide,
not an additional acceptance claim.

权威验收状态请以 Workspace lifecycle plan 和 Phase 0 evidence record 为准；本 README 只是
操作连接指南，不新增任何验收结论。
