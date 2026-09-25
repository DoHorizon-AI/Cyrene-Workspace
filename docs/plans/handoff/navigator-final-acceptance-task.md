# Navigator final acceptance task / Navigator 最后验收交接

English scope: finish the remaining Navigator acceptance and delivery work from
the existing V8 evidence. Reactor deployment, Platform GPU execution and Yield
training have other owners. The Chinese instructions below are the executable
handoff; no prior conversation is required.

以下全文可直接转交给接手模型。核对日期：2026-09-06；SHA 是定位线索，开始执行时必须
重新读取实际分支、PR 和 CI，不把本文件当成永久有效的远程状态。

## Task prompt / 执行提示词

你负责 Cyrene Navigator 已有 V8 的最后验收和交付。使用中文汇报，先阅读实际可用且相关的
dev-suite、jetbrains-ide、git-delivery、code-style、lang-conventions、j-space Skills。
使用当前 IDE 已索引的目录，不创建 worktree，不覆盖已有改动，不在 main/develop 累积开发。
一个仓库同时只有一个写入负责人；源码、Git、格式化、锁文件和构建均算写入。
你拥有 Navigator 仓库的写入权；Workspace 证据更新须先与主 Agent 协调写入时段。
Platform、Exchange、Plugins、Reactor、Yield 默认只读，需要修改时提交文件、接口、原因和
验收方式，由主 Agent 协调。不得接管 Reactor 部署、GPU 适配、模型产物契约或 Yield 训练。

仓库入口：`/home/baijin/Dev/Cyrene/Cyrene-Workspace/repositories.yaml`。
Navigator：`/home/baijin/Dev/Cyrene/Cyrene-Services/Cyrene-Navigator`。
先读 Workspace 的以下文件，再读 Navigator 对应实现和验收脚本：

1. `docs/plans/cyrene-text-model-lifecycle-v1.md`，重点 P0-12、P0-29、P0-GATE。
2. `docs/plans/evidence/2026-09-06-phase-0.md`，优先 Current evidence delta 和
   V8 frozen backends and handoff；较早失败记录保留为历史，不重复计入当前状态。
3. `docs/plans/handoff/cyrene-navigator-v8/README.md`、`WINDOWS_ACCEPTANCE.md`、
   `BACKENDS.md`、`Connect-Navigator.ps1` 和 `connection.example.json`。
4. `governance/product-contract-v1/navigator-harness-v1.md`。

### Verified starting point / 已核对的起点

| Repository / 仓库 | Task HEAD / 任务提交 | Delivery / 交付快照 |
| --- | --- | --- |
| Navigator | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` | `feat/text-model-lifecycle-v1` 已推送；本次查询无 open PR，不能虚构 PR 或合并 |
| Workspace | `2d79c77d01760bce0cd7455b8457b7d00ef7d2b9` | 同名任务分支，Draft PR #4 → main；本交接文档会产生后续提交 |
| Platform | `30272145b9c11df9948465359479c3d95d08a1dc` | Draft PR #31 堆叠在 #30，均未合并 |
| Plugins | `261a78fd36c7cb2da85a504a89ec7c9c353ecc19` | Draft PR #9 → develop |
| Exchange | `91c509904add26de941e2e14bc3b2b4752233d28` | Draft PR #7 → develop |

V8 使用 DeepSeek Harness `dsh-v0.1.3-alpha.1` /
`d347e703908d0406b7a7ef80e3a0e594d86b2215`，Core patches = 0。
NSIS SHA-256：`792d21adbbc2b95945dd61bfe43c68097a3d93d1f987dcb6e1cbc07687943a90`；
已安装 executable SHA-256：
`0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1`。
包内 cy-manifest 来源仍是 Platform `185527f82c83700d4f567ac563a393a2e1c18c87`，
不得替换成 Linux CES 的 SHA 来描述包内 provenance。

当前 Phase 0 为 30/32 scoped Actual PASS。V8 已有干净 Windows Guest 安装、真实
模型→Rust Tool→模型续答、正常关闭、持久化服务故障与恢复、显式 takeover、旧 writer
拒绝、retained input Restore 后显式 Send 的证据。两次正常关闭为 925 ms、868 ms，
9 个 owned processes 和原 listeners 归零。最终冻结后端复验达到 90 events、5 Tools、
57 Exchange requests，前 77 events 完整保留。不要为重新证明这些已通过场景展开重构。

原始收据位于 Navigator `.navigator/proof/` 和 Windows
`C:\cyrene-nav-build\.task-cache\phase0-windows\`。先核对收据哈希和工具说明；其中可能
存在私有会话数据，禁止把原始数据库、完整会话、ready URL 或凭据提交或打包。

### Remaining work / 剩余任务

1. **P0-29：同一 V8 的升级失败诊断。** 在可恢复的隔离 Windows Guest/snapshot 中，
   使用真实 installer/update 路径产生一次受控失败。验证错误可定位、已安装资源和用户
   数据的边界、恢复动作、恢复后启动；区分正常关闭与故障注入清理。不得用制造一条
   error 日志、单元测试、损坏同事环境或 kill 进程代替验收。若需要改源码，固定新提交
   和新安装包，重新验证受到影响的场景；不能把 V8 收据改写成新包的证据。
2. **P0-12 / P1-12：外部 Provider 独立入口。** 复用已固定 Harness 的 provider/profile
   能力，用现有且授权的外部模型服务完成真实聊天。明确来源不经过 Exchange、usage
   owner 和 credential 边界；Cyrene persistence 与单一 session history authority
   保持一致。证明认证失败有明确反馈、不会静默回落到 Exchange；adoption proof 本身
   仍以已有 Exchange 路径为准。没有可用凭据时准确列出外部依赖，继续其他本地工作。
3. **交付包与说明。** 核实私有交付包是否已实际生成，检查 exact NSIS、release-lock.json、
   SOURCE_VERSIONS.md、SHA256SUMS、连接脚本、完整 runtime 内容图和脱敏验收说明。
   完整目录 parity 的历史基线是 24,382 files。仅在包或文档有变化时更新相关内容及
   校验和；不得称客户端包包含或部署了 Exchange、persistence、Reactor、vLLM 或 GPU。
4. **阶段决定。** 逐条按现有 P0-GATE 定义判定，不自行降低门槛。P0-12 在原计划中不阻塞
   Exchange adoption proof，但仍是待完成的独立能力；不能为凑 32/32 调整规则。

### Delivery and acceptance / 交付与验收

Workspace run `34044372662` 的 job `101516676416` 为 0 steps，GitHub annotation 明确为
payments/spending limit；这是实时核对的外部 CI 阻塞，不是源码测试失败。不提高支出、
扩大权限、反复重跑或弱化检查；继续本地可执行工作。其他 PR/CI 开始时重新核对。

正常提交、推送、建立或更新 PR；遵守现有合并授权，无授权不自行合并。不得 force push、
重写共享历史、管理员绕过或跳过测试。不要将本地通过描述为远端通过或 canonical 接受。
外部组件以实际采用版本核对 LICENSE、依赖和维护状态。

每项证据记录：exact SHA/安装包 digest、命令、环境、pass/fail/skip 数量、收据位置与
哈希、已知限制。仅修复安全、数据丢失、错误执行或当前用户路径的阻塞；其他发现列为
后续事项。仅在阶段变化、新阻塞、决策和完成时中文汇报。

最终交付给用户：分支和精确 SHA、变更文件、可执行验证步骤、实际结果、PR/CI 状态、
安装包位置及 digest、P0-12/P0-29/P0-GATE 分项状态、下一位接手者需要的接口。
完成上述任务后停止扩展。
---
<!-- Chinese Translation / 中文翻译 -->

# Navigator 最终验收任务

## 范围

根据现有 V8 证据，完成 Navigator 剩余的验收和交付工作。Reactor 部署、Platform GPU 执行和 Yield 训练由其他负责人承担。下方中文说明可直接作为交接任务，不依赖此前对话。

以下全文可直接转交给接手模型。核对日期：2026-09-06；SHA 只是定位线索，开始执行时必须重新读取实际分支、PR 和 CI，不能把本文件当成永久有效的远端状态。

## 任务提示

你负责完成 Cyrene Navigator 现有 V8 的最终验收和交付。使用中文汇报；先阅读当前实际可用且与任务相关的 dev-suite、jetbrains-ide、git-delivery、code-style、lang-conventions 和 j-space Skills。

使用当前 IDE 已索引的目录，不创建 worktree、不覆盖已有改动，也不在 main/develop 上累积开发。每个仓库同时只能有一个写入负责人；源码、Git、格式化、锁文件和构建都算写入操作。你有权修改 Navigator 仓库；更新 Workspace 证据前，须先与主 Agent 协调写入时段。Platform、Exchange、Plugins、Reactor 和 Yield 默认只读；若需要修改，先向主 Agent 提交文件、接口、原因和验收方式，由其协调。不得接管 Reactor 部署、GPU 适配、模型制品合约或 Yield 训练。

仓库入口：`/home/baijin/Dev/Cyrene/Cyrene-Workspace/repositories.yaml`。
Navigator：`/home/baijin/Dev/Cyrene/Cyrene-Services/Cyrene-Navigator`。
先阅读 Workspace 下列文件，再阅读 Navigator 对应实现和验收脚本：

1. `docs/plans/cyrene-text-model-lifecycle-v1.md`，重点查看 P0-12、P0-29、P0-GATE。
2. `docs/plans/evidence/2026-09-06-phase-0.md`，优先查看 Current evidence delta 和 V8 frozen backends and handoff；早期失败记录只保留为历史，不重复计入当前状态。
3. `docs/plans/handoff/cyrene-navigator-v8/README.md`、`WINDOWS_ACCEPTANCE.md`、`BACKENDS.md`、`Connect-Navigator.ps1` 和 `connection.example.json`。
4. `governance/product-contract-v1/navigator-harness-v1.md`。

### 已核对的起点

| 仓库 | 任务 HEAD / 任务提交 | 交付快照 |
|---|---|---|
| Navigator | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` | `feat/text-model-lifecycle-v1` 已推送；本次查询没有开放 PR，不得虚构 PR 或合并 |
| Workspace | `2d79c77d01760bce0cd7455b8457b7d00ef7d2b9` | 同名任务分支，Draft PR #4 → main；本交接文档会产生后续提交 |
| Platform | `30272145b9c11df9948465359479c3d95d08a1dc` | Draft PR #31 依赖 #30，均未合并 |
| Plugins | `261a78fd36c7cb2da85a504a89ec7c9c353ecc19` | Draft PR #9 → develop |
| Exchange | `91c509904add26de941e2e14bc3b2b4752233d28` | Draft PR #7 → develop |

V8 使用 DeepSeek Harness `dsh-v0.1.3-alpha.1` / `d347e703908d0406b7a7ef80e3a0e594d86b2215`，Core patches = 0。NSIS SHA-256：`792d21adbbc2b95945dd61bfe43c68097a3d93d1f987dcb6e1cbc07687943a90`；已安装 executable SHA-256：`0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1`。包内 cy-manifest 的来源仍是 Platform `185527f82c83700d4f567ac563a393a2e1c18c87`；不得以 Linux CES 的 SHA 描述包内 provenance。

当前 Phase 0 为 30/32 scoped Actual PASS。V8 已有干净 Windows Guest 安装、真实模型 → Rust Tool → 模型续答、正常关闭、持久化服务故障与恢复、显式 takeover、旧 writer 拒绝，以及 retained input Restore 后显式 Send 的证据。两次正常关闭用时为 925 ms 和 868 ms，9 个 owned processes 与原 listeners 均归零。最终冻结后端复验达到 90 events、5 Tools、57 Exchange requests，前 77 events 完整保留。不要为了重新证明这些已通过的场景而展开重构。

原始收据位于 Navigator `.navigator/proof/` 和 Windows `C:\cyrene-nav-build\.task-cache\phase0-windows\`。先核对收据哈希和工具说明；其中可能有私人会话数据，禁止提交或打包原始数据库、完整会话、ready URL 或凭据。

### 剩余任务

1. **P0-29：对同一 V8 执行升级失败诊断。**在可恢复的隔离 Windows Guest/snapshot 中，通过真实 installer/update 路径制造一次受控失败。验证错误可定位、已安装资源与用户数据的边界、恢复动作以及恢复后的启动；区分正常关闭和故障注入清理。不得用伪造 error 日志、单元测试、破坏同事环境或 kill 进程来替代验收。如果必须修改源码，固定新提交和新安装包，并重新验证受影响场景；不得把 V8 收据改写成新包的证据。
2. **P0-12 / P1-12：外部 Provider 独立入口。**复用已固定 Harness 的 provider/profile 能力，通过现有且获授权的外部模型服务完成真实聊天。明确数据来源不经过 Exchange，并厘清 usage owner 与 credential 边界；Cyrene persistence 仍须保持单一 session history authority。证明认证失败会明确反馈且不会静默回退到 Exchange；adoption proof 本身仍以已有 Exchange 路径为准。若没有可用凭据，准确列出外部依赖，并继续其他本地工作。
3. **交付包与说明。**核实私有交付包是否实际生成；检查 exact NSIS、release-lock.json、SOURCE_VERSIONS.md、SHA256SUMS、连接脚本、完整 runtime 内容映射和脱敏验收说明。完整目录 parity 的历史基线为 24,382 files。只有包或文档发生变化时才更新相关内容和校验和；不得声称客户端包包含或部署了 Exchange、persistence、Reactor、vLLM 或 GPU。
4. **阶段决策。**逐项按照现有 P0-GATE 定义判定，不自行降低门槛。原计划中 P0-12 不阻塞 Exchange adoption proof，但它仍是待完成的独立能力；不得为了凑足 32/32 而修改规则。

### 交付与验收

Workspace run `34044372662` 的 job `101516676416` 为 0 steps；GitHub annotation 明确指出这是 payments/spending limit。它是实时核实的外部 CI 阻塞，不是源码测试失败。不要提高支出、扩大权限、反复重跑或弱化检查；继续本地可执行工作。开始处理其他 PR/CI 时重新核对状态。

按正常流程提交、推送、建立或更新 PR，并遵守现有合并授权；没有授权不得自行合并。不得 force push、重写共享历史、管理员绕过或跳过测试。不得把本地通过描述为远端通过或 canonical 接受。外部组件的 LICENSE、依赖和维护状态应依据实际采用的版本核对。

每项证据都记录 exact SHA/安装包 digest、命令、环境、pass/fail/skip 数量、收据位置与哈希以及已知限制。只修复安全、数据丢失、错误执行或当前用户路径上的阻塞；其他发现列为后续事项。只在阶段变化、新阻塞、决策和完成时用中文汇报。

最终交付给用户：分支和精确 SHA、变更文件、可执行验证步骤、实际结果、PR/CI 状态、安装包位置及 digest、P0-12/P0-29/P0-GATE 各项状态，以及下一位接手者需要的接口。完成后停止扩展。
