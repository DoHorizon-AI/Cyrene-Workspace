# Navigator V8 Windows acceptance / Windows 验收

The requested Windows close-out scenarios passed on the same V8 NSIS package.
This is a private Alpha handoff, with separately operated backends. Exact source,
branch, dependency and binary identities are recorded in `release-lock.json`
and `SOURCE_VERSIONS.md` in the delivery archive.

本次要求的 Windows 收尾场景已在同一份 V8 NSIS 包上通过。交付对象是私有 Alpha 客户端，
后端独立运行；交付压缩包内的 `release-lock.json` 和 `SOURCE_VERSIONS.md` 固定完整源码
SHA、分支状态、依赖与二进制身份。

| Item / 项目 | Exact identity / 精确身份 |
| :--- | :--- |
| Navigator source | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` |
| V8 NSIS SHA-256 | `792d21adbbc2b95945dd61bfe43c68097a3d93d1f987dcb6e1cbc07687943a90` |
| Installed executable SHA-256 | `0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1` |
| DeepSeek Harness | `dsh-v0.1.3-alpha.1` / `d347e703908d0406b7a7ef80e3a0e594d86b2215` |
| Upstream Core patches | `0` |
| Environment | Isolated Windows 11 guest; no Node, Rust or Python development environment, no system VC runtime installation / 隔离 Windows 11 Guest，无 Node、Rust、Python 开发环境，无额外系统 VC runtime 安装 |

---

## Behavioral Acceptance Evidence Index / 核心行为验收证据链索引

为确保所有验收行为均具备直接可追溯的收据、执行环境与结论，下表对各关键行为证据进行逐项索引：

| 验收行为目标 | 证据收据文件路径 | 收据 SHA-256 校验和 | 测试环境与触发方式 | 实测指标与结论 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 干净 Windows Guest 正常关闭及进程回收** | `.navigator/proof/windows-v8-cleanvm-normal-close.json`<br><br>*(备用复验: `windows-v8-cleanvm-close-after-outage.json`)* | `2312828d562c894aeba335acda17a50a6ebc37b31110f328c79ea358dd488b6f`<br><br>`437f63f65c47aa1a30febeab630672be83425d72d4fd9c472e13bc6d28922ebd` | 干净 Windows 11 Guest VM（无运行时库）。向可见主 Tauri 窗口投递正常 `WM_CLOSE`（排除 Tao 消息窗口）。 | **925 ms** 正常退出（复验 868 ms）。9 个 owned 进程与 CDP/Harness 监听器完全归零，退出码 0，未调用 taskkill / SIGKILL 强制清理。<br>👉 **ACTUAL PASS** |
| **2. persistence 服务重启后的恢复与续聊** | `.navigator/proof/windows-v8-persistence-outage.json`<br><br>`.navigator/proof/windows-v8-frozen-backends-tool-audit.json` | `f31289cf1a1132c3008985161cf7520e5d8b76313b2c15fbc70519ddac945fa6`<br><br>`48a202df3f48a17beec4b80261175eb82bcebc6d628c5fe1190539ecb71a06a5` | 运行中的 persistence 进程遭遇 SIGKILL 异常中断（PID 1351755 退出）；随后在同一 SQLite 数据库下以 PID 1462232 重启。 | 故障期间数据完整停留在 68 events、4 Tools、54 requests；重启后客户端重连完整读取前缀并续聊，推进至 77 events、55 requests，冻结后端复验推进至 90 events、5 Tools、57 requests。<br>👉 **ACTUAL PASS** |
| **3. 接管后旧 writer 被拒绝 (Fencing)** | `.navigator/proof/windows-v8-stale-writer-audit-check.json`<br><br>*(详细记录: `windows-v8-host-takeover-summary.json`)* | `a512ec2922e1b08a3e5aa7c4335fa9b067c4aed8941253ec77c2187fa3cd685d`<br><br>`55be326e4e8ff78bca612030ee9a2ba33527e2c94ca1e626e2798e9a263fa7db` | Guest 客户端通过 UI 显式接管（Writer Epoch 3→4，重启后推进至 5→6）。随后原 Host 客户端尝试通过 GUI 发送新消息。 | 旧 writer 请求被服务端严格拒绝（返回租约冲突 `SessionAlreadyOwnedError`）；服务端审计证实会话记录保持为 67 events、54 requests，历史未受任何破坏。<br>👉 **ACTUAL PASS** |
| **4. 历史 Tool 不重放** | `.navigator/proof/guest-v8-frozen-backend-tool-summary.json`<br><br>*(格式导入复验: `windows-codex-ui-result.json`)* | `96a798b584d4fb4c09d571871239c0fc33568c0b2b8c9d1a8e1858a2d1dcb727`<br><br>`0ce37c1569c792942442cf28238ba43ea23cfbc80e8c89b7bc230fb1dbcd1bf1` | 会话读取、断线恢复、Takeover 接管与导入历史会话场景。 | 历史 Tool Call 与 Tool Result 均以只读状态载入（`executable: false`），未触发重放；Tool 执行计数严格只在显式发起新工具轮次时从 4 递增至 5。<br>👉 **ACTUAL PASS** |
| **5. 未确认输入显式恢复、发送** | `.navigator/proof/guest-v8-input-recovery-summary.json`<br><br>*(离线审计: `windows-v8-offline-input-backend-check.json`)* | `cd488907157aa5ba2f811d462455cc5d1ff4701a3cdceb6a6eacb9441b7a8542`<br><br>`94b1da93fca1dbf2ca6da4d6ea0b0ae19b8f2d6582490b4fe005d53cbfa5b0d0` | 输入未提交内容后模拟异常崩溃与客户端退出；重新打开后检查界面提示并点击 **Restore input**，核对文本后点击 **Send**。 | 恢复前服务端历史不变；点击 Restore 无损还原草稿；点击 Send 推进至 77 events；收到 receipt 后本地 retained copy 立即清零。<br>👉 **ACTUAL PASS** |

---

## Accepted scenarios / 已通过场景总结

- [x] **Normal close / 正常退出**：投递 `WM_CLOSE` 仅定位可见 Tauri 主窗口，排除内部消息窗口，925 ms / 868 ms 退出，9 个所属进程及监听器全部归零。
- [x] **Persistence outage and recovery / 持久化服务中断与恢复**：真实 persistence 进程 SIGKILL 中断并重启，完整读取 68 个前缀事件，顺利推进续聊至 77→90 events。
- [x] **Explicit takeover / 显式接管**：客户端显式递增 Epoch（3→4→5→6），旧写者发送被严格拦截拒绝，会话历史未遭污染。
- [x] **No historical Tool execution / 历史 Tool 不重放**：历史工具调用只读呈现（`executable: false`），恢复时不重复执行本地工具。
- [x] **Input recovery / 输入恢复**：未提交输入经崩溃重开后显式 Restore 还原，显式 Send 后提交并清空本地保留副本。

---

## Staged and Installed Runtime Parity / 运行时完整度校验

The full staged and installed guest runtime trees match: **24,382 files**, no missing, additional or changed files.
- **Content Map SHA-256**: `1b2fd08a483fc050e6cd0e6cdfd477220d39f06f10f0b085899ec9505fe096a3`
- **Comparison Receipt**: `.navigator/proof/windows-v8-complete-runtime-parity.json` (SHA-256: `6b919ec0874b7a1c74dd8f6d0377cb709a83dca3105975227e9f9339db740b9c`)

---

## Limits & Deferred Scope / 限制与延期说明

- **未签名安装包与安装路径**：客户端未作代码签名，验收采用 NSIS 默认每用户安装路径；MSI 包哈希供归档备查，不作为本轮主路径。
- **P0-29 升级失败受控注入诊断 (DEFERRED)**：
  - **当前状态**：`DEFERRED (待在后续发布验证环境中注入测试)`。
  - **事实说明**：当前 V8 为私有受控内部试用 Alpha 包，干净 Windows VM 的安装与正常启停已通过；但尚未接入升级发布通道，亦未在隔离测试副本中模拟新旧版本覆盖失败与回滚的受控注入。该项明确列入待测延期项，不虚报为通过。
- **P0-12 外部商业 Provider 独立隔离入口 (DEFERRED_TO_PHASE_1)**：
  - **当前状态**：`DEFERRED_TO_PHASE_1`。
  - **事实说明**：按产品契约，DSH → Exchange 采纳证明已闭环；直接跳过 Exchange 连接第三方商业 Provider 属于个人 Profile 独立能力，依赖外部商业凭据，已正式延期至 Phase 1 / P1-12 跟踪，不作为阻塞 Phase 0 采纳门禁的条件。
- **远端 CI 计费阻断 (BLOCKED_EXTERNAL)**：
  - GitHub Actions 因组织 payments / spending limit 导致任务步骤未执行（0 steps），记为 `BLOCKED_EXTERNAL`，不以旧提交绿灯冒充新包。
