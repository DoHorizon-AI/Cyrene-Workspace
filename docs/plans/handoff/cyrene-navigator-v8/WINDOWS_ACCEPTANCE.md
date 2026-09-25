# Navigator V8 Windows acceptance / Windows 验收

The requested Windows close-out scenarios passed on the same V8 NSIS package.
This is a private Alpha handoff, with separately operated backends. Exact source,
branch, dependency, receipt, and binary identities are recorded in `release-lock.json`
and `SOURCE_VERSIONS.md` in the delivery archive.

本次要求的 Windows 收尾场景已在同一份 V8 NSIS 包上通过。交付对象是私有 Alpha 客户端，
后端独立运行；交付归档内的 `release-lock.json` 和 `SOURCE_VERSIONS.md` 固定完整源码
SHA、分支状态、依赖与二进制身份。关键行为收据副本已归档至同级目录 `proof/`。

| Item / 项目 | Exact identity / 精确身份 |
| :--- | :--- |
| Navigator source | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` |
| V8 NSIS SHA-256 | `792d21adbbc2b95945dd61bfe43c68097a3d93d1f987dcb6e1cbc07687943a90` |
| Installed executable SHA-256 | `0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1` |
| DeepSeek Harness | `dsh-v0.1.3-alpha.1` / `d347e703908d0406b7a7ef80e3a0e594d86b2215` |
| Upstream Core patches | `0` |
| Environment | Isolated Windows 11 guest; no Node, Rust or Python development environment, no system VC runtime installation / 隔离 Windows 11 Guest，无 Node、Rust、Python 开发环境，无额外系统 VC runtime 安装 |

---

## 1. Acceptance Stratification and Status Summary / 验收分层与状态汇总

为避免计数混淆与口径矛盾，验收状态严格采用三层分级表达：

| 分层维度 | 检查项范畴 | 统计结果 | 具体说明 |
| :--- | :--- | :--- | :--- |
| **层级一：原 32 项功能与行为检查** (P0-01 ~ P0-32) | 全生命周期功能与集成测试清单 | **30 项 Actual PASS**<br>**2 项批准延期 (DEFERRED_APPROVED)**<br>*(合计 32 项)* | 30 项已有完整本地/虚拟机实测数据与收据支持；P0-12 与 P0-29 经确认属于范围边界调整，正式批准延期。 |
| **层级二：当前交付范围内尚待行为验收** | 本轮受控内部试用交付路径 | **0 项 (NONE_PENDING)** | 本轮内部受控试用所承诺的 30 项功能及 5 项关闭缺口均已具备闭环收据支撑。 |
| **层级三：交付外部门禁与最终发布判定** | 远端 CI 状态与 Canonical 判定 | **Hosted CI: BLOCKED_EXTERNAL**<br>**Canonical 终审: PENDING** | GitHub Actions 0 steps 确认为计费限额阻断；待外部计费恢复后由主 Agent 进行主线合并与 Canonical 终审判定。 |

### 延期项目定性明细 (Deferred Scope Classification)

- **P0-12 (直接连接外部商业 Provider 独立入口)**:
  - **执行事实 (Execution Fact)**: `NOT_TESTED` (外部第三方商业 API 凭据未在当前环境提供)
  - **本次交付范围 (Delivery Scope)**: `DEFERRED_APPROVED` (直接外部 Provider 属于后续独立能力，不影响本次 DSH → Exchange 采纳证明)
  - **后续归属 (Subsequent Tracking)**: Phase 1 / P1-12
- **P0-29 (升级失败受控注入诊断)**:
  - **执行事实 (Execution Fact)**: `NOT_TESTED` (尚未在隔离测试副本中模拟人为中断升级文件的覆盖破坏注入)
  - **本次交付范围 (Delivery Scope)**: `DEFERRED_APPROVED` (当前 V8 为内部受控试用 Alpha 包，尚未接入自动升级服务，经批准延期)
  - **后续归属 (Subsequent Tracking)**: 正式升级分发与自动更新验收阶段

---

## 2. Behavioral Acceptance Evidence Index / 核心行为验收证据链索引

所有收据实体已脱敏并保存在本交接目录的 `proof/` 子目录下，同时附带开发阶段收据路径以供比对：

| 验收行为目标 | 交付收据文件相对路径 (proof/) | 收据 SHA-256 校验和 | 测试环境与触发方式 | 实测指标与结论 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 干净 Windows Guest 正常关闭及所属进程回收** | `proof/windows-v8-cleanvm-normal-close.json`<br><br>*(复验: `proof/windows-v8-cleanvm-close-after-outage.json`)* | `2312828d562c894aeba335acda17a50a6ebc37b31110f328c79ea358dd488b6f`<br><br>`437f63f65c47aa1a30febeab630672be83425d72d4fd9c472e13bc6d28922ebd` | 干净 Windows 11 Guest VM（无运行时库）。向可见主 Tauri 窗口投递正常 `WM_CLOSE`（排除内部 Tao 消息窗口）。 | **925 ms** 正常退出（复验 868 ms）。监测到的 9 个 owned 进程与 CDP/Harness 监听器完全归零，退出码 0，未触发强制 kill 进程。<br>👉 **ACTUAL PASS** |
| **2. persistence 服务重启后的恢复与续聊** | `proof/windows-v8-persistence-outage.json`<br><br>`proof/windows-v8-frozen-backends-tool-audit.json` | `f31289cf1a1132c3008985161cf7520e5d8b76313b2c15fbc70519ddac945fa6`<br><br>`48a202df3f48a17beec4b80261175eb82bcebc6d628c5fe1190539ecb71a06a5` | 运行中的 persistence 进程遭遇 SIGKILL 异常中断（PID 1351755 退出）；随后在同一 SQLite 数据库下以 PID 1462232 重启。 | 故障期间数据完整停留在 68 events、4 Tools、54 requests；重启后客户端重连完整读取前缀并续聊，推进至 77 events、55 requests，冻结后端复验推进至 90 events、5 Tools、57 requests。<br>👉 **ACTUAL PASS** |
| **3. 接管后旧 writer 被拒绝 (Fencing)** | `proof/windows-v8-stale-writer-audit-check.json`<br><br>*(详细记录: `proof/windows-v8-host-takeover-summary.json`)* | `a512ec2922e1b08a3e5aa7c4335fa9b067c4aed8941253ec77c2187fa3cd685d`<br><br>`55be326e4e8ff78bca612030ee9a2ba33527e2c94ca1e626e2798e9a263fa7db` | Guest 客户端通过 UI 显式接管（Writer Epoch 3→4，重启后推进至 5→6）。随后原 Host 客户端尝试通过 GUI 发送新消息。 | 旧 writer 请求被服务端严格拒绝（返回租约冲突 `SessionAlreadyOwnedError`）；服务端审计证实会话记录保持为 67 events、54 requests，历史未受任何污染破坏。<br>👉 **ACTUAL PASS** |
| **4. 历史 Tool 不重放** | `proof/guest-v8-frozen-backend-tool-summary.json` | `96a798b584d4fb4c09d571871239c0fc33568c0b2b8c9d1a8e1858a2d1dcb727` | 会话读取、断线恢复、Takeover 接管与导入历史会话场景。 | 历史 Tool Call 与 Tool Result 均以只读状态载入（`executable: false`），未触发重放；Tool 执行计数严格只在显式发起新工具轮次时从 4 递增至 5。<br>👉 **ACTUAL PASS** |
| **5. 未确认输入显式恢复、发送** | `proof/guest-v8-input-recovery-summary.json` | `cd488907157aa5ba2f811d462455cc5d1ff4701a3cdceb6a6eacb9441b7a8542` | 输入未提交内容后模拟异常崩溃与客户端退出；重新打开后检查界面提示并点击 **Restore input**，核对文本后点击 **Send**。 | 恢复前服务端历史不变；点击 Restore 无损还原草稿；点击 Send 推进至 77 events；收到 receipt 后本地 retained copy 立即清零。<br>👉 **ACTUAL PASS** |

---

## 3. Staged and Installed Runtime Parity / 运行时完整度校验

The full staged and installed guest runtime trees match: **24,382 files**, no missing, additional or changed files.
- **Content Map SHA-256**: `1b2fd08a483fc050e6cd0e6cdfd477220d39f06f10f0b085899ec9505fe096a3`
- **Comparison Receipt**: `proof/windows-v8-complete-runtime-parity.json` (SHA-256: `6b919ec0874b7a1c74dd8f6d0377cb709a83dca3105975227e9f9339db740b9c`)

---

## 4. Limits & Boundary Notes / 限制与边界说明

1. **未签名客户端**：私有试用客户端未做数字签名，验收基于 NSIS 默认按用户（per-user）安装路径；MSI 包仅作为备选产物记录。
2. **后置依赖服务**：Exchange 与 Persistence 服务需预先部署就绪，客户端安装包不包含也不部署后端、Reactor、vLLM 或 GPU 驱动。
3. **远端 CI 计费限制**：GitHub Actions 因外部 spending limit 处于 0 steps，明确标记为 `BLOCKED_EXTERNAL`，不以旧提交绿灯覆盖新构建包。
4. **测试隐私保护**：归档在 `proof/` 的所有收据均已执行脱敏处理，排除了数据库连接串、未哈希 token、账号凭据及原始对话全量内容。
---
<!-- Chinese Translation / 中文翻译 -->

# Navigator V8 Windows 验收

本次要求的 Windows 收尾场景已在同一份 V8 NSIS 包上通过。这是一个私有 Alpha 客户端交接，后端由其他服务独立运行。交付归档中的 `release-lock.json` 和 `SOURCE_VERSIONS.md` 记录精确的源码、分支、依赖、收据和二进制身份。

| 项目 | 精确身份 |
|---|---|
| Navigator 源码 | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` |
| V8 NSIS SHA-256 | `792d21adbbc2b95945dd61bfe43c68097a3d93d1f987dcb6e1cbc07687943a90` |
| 已安装可执行文件 SHA-256 | `0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1` |
| DeepSeek Harness | `dsh-v0.1.3-alpha.1` / `d347e703908d0406b7a7ef80e3a0e594d86b2215` |
| 上游 Core patches | `0` |
| 环境 | 隔离的 Windows 11 guest；未安装 Node、Rust 或 Python 开发环境，也未安装系统 VC runtime |

## 1. 验收分层与状态汇总

为避免计数混淆与口径矛盾，验收状态严格采用三层分级表达：

| 分层维度 | 检查项范畴 | 统计结果 | 具体说明 |
|---|---|---|---|
| **层级一：原 32 项功能与行为检查**（P0-01 ~ P0-32）| 全生命周期功能与集成测试清单 | **30 项 Actual PASS**；**2 项批准延期（DEFERRED_APPROVED）**；合计 32 项 | 30 项已有完整本地 / 虚拟机实测数据与收据支持；P0-12 与 P0-29 经确认属于范围边界调整，正式批准延期。|
| **层级二：当前交付范围内尚待行为验收** | 本轮受控内部试用交付路径 | **0 项（NONE_PENDING）** | 本轮内部受控试用所承诺的 30 项功能及 5 项关闭缺口均已有闭环收据支撑。|
| **层级三：交付外部门禁与最终发布判定** | 远端 CI 状态与 Canonical 判定 | **Hosted CI: BLOCKED_EXTERNAL**；**Canonical 终审: PENDING** | 已确认 GitHub Actions 的 0 steps 是计费限额导致；待外部计费恢复后，由主 Agent 执行主线合并和 Canonical 终审。|

### 延期范围的分类

- **P0-12（直接连接外部商业 Provider 的独立入口）**：
  - **执行事实**：`NOT_TESTED`（当前环境未提供外部第三方商业 API 凭据）。
  - **本次交付范围**：`DEFERRED_APPROVED`（直接外部 Provider 属于后续独立能力，不影响本次 DSH → Exchange 采纳证明）。
  - **后续跟踪**：Phase 1 / P1-12。
- **P0-29（升级失败的受控注入诊断）**：
  - **执行事实**：`NOT_TESTED`（尚未在隔离测试副本中模拟人为中断升级文件覆盖的破坏性注入）。
  - **本次交付范围**：`DEFERRED_APPROVED`（当前 V8 是内部受控试用 Alpha 包，尚未接入自动升级服务，经批准延期）。
  - **后续跟踪**：正式升级分发与自动更新验收阶段。

## 2. 核心行为验收证据索引

所有收据实体均已脱敏，保存在本交接目录的 `proof/` 子目录下，同时附有开发阶段收据路径供比对：

| 验收行为目标 | 交付收据文件相对路径（proof/）| 收据 SHA-256 校验和 | 测试环境与触发方式 | 实测指标与结论 |
|---|---|---|---|---|
| **1. 干净 Windows Guest 正常关闭及所属进程回收** | `proof/windows-v8-cleanvm-normal-close.json`（复验：`proof/windows-v8-cleanvm-close-after-outage.json`）| `2312828d562c894aeba335acda17a50a6ebc37b31110f328c79ea358dd488b6f`；`437f63f65c47aa1a30febeab630672be83425d72d4fd9c472e13bc6d28922ebd` | 干净的 Windows 11 Guest VM（无运行时库）；向可见的主 Tauri 窗口发送正常 `WM_CLOSE`（排除内部 Tao 消息窗口）。| 正常退出用时 **925 ms**（复验为 868 ms）。监测到的 9 个所属进程及 CDP/Harness 监听器全部归零，退出码为 0，未触发强制 kill 进程。**ACTUAL PASS** |
| **2. Persistence 服务重启后的恢复与续聊** | `proof/windows-v8-persistence-outage.json`；`proof/windows-v8-frozen-backends-tool-audit.json` | `f31289cf1a1132c3008985161cf7520e5d8b76313b2c15fbc70519ddac945fa6`；`48a202df3f48a17beec4b80261175eb82bcebc6d628c5fe1190539ecb71a06a5` | 运行中的 persistence 进程遭遇 SIGKILL 异常中断（PID 1351755 退出）；随后在同一 SQLite 数据库上以 PID 1462232 重启。| 故障期间数据完整停留在 68 events、4 Tools、54 requests；重启后客户端重连、完整读取前缀并续聊，推进至 77 events、55 requests；冻结后端复验推进至 90 events、5 Tools、57 requests。**ACTUAL PASS** |
| **3. 接管后拒绝旧 writer（Fencing）** | `proof/windows-v8-stale-writer-audit-check.json`（详细记录：`proof/windows-v8-host-takeover-summary.json`）| `a512ec2922e1b08a3e5aa7c4335fa9b067c4aed8941253ec77c2187fa3cd685d`；`55be326e4e8ff78bca612030ee9a2ba33527e2c94ca1e626e2798e9a263fa7db` | Guest 客户端通过 UI 显式接管（Writer Epoch 3→4，重启后推进至 5→6）。随后原 Host 客户端尝试通过 GUI 发送新消息。| 服务端严格拒绝旧 writer 请求（返回租约冲突 `SessionAlreadyOwnedError`）；审计证实会话记录仍为 67 events、54 requests，历史未受污染或破坏。**ACTUAL PASS** |
| **4. 不重放历史 Tool** | `proof/guest-v8-frozen-backend-tool-summary.json` | `96a798b584d4fb4c09d571871239c0fc33568c0b2b8c9d1a8e1858a2d1dcb727` | 会话读取、断线恢复、Takeover 接管和历史会话导入场景。| 历史 Tool Call 与 Tool Result 均以只读状态载入（`executable: false`），未触发重放；只有显式启动新的工具轮次时，Tool 执行计数才从 4 增至 5。**ACTUAL PASS** |
| **5. 显式恢复并发送未确认输入** | `proof/guest-v8-input-recovery-summary.json` | `cd488907157aa5ba2f811d462455cc5d1ff4701a3cdceb6a6eacb9441b7a8542` | 输入未提交内容后模拟异常崩溃和客户端退出；重新打开后检查界面提示，点击 **Restore input**，核对文本后点击 **Send**。| 恢复前服务端历史不变；点击 Restore 无损还原草稿；点击 Send 后推进至 77 events；收到 receipt 后本地 retained copy 立即清零。**ACTUAL PASS** |

## 3. 暂存与已安装运行时一致性

完整的暂存 guest runtime 与已安装 guest runtime 目录树完全一致：**24,382 个文件**，没有缺失、新增或变更文件。

- **内容映射 SHA-256**：`1b2fd08a483fc050e6cd0e6cdfd477220d39f06f10f0b085899ec9505fe096a3`
- **比对收据**：`proof/windows-v8-complete-runtime-parity.json`（SHA-256：`6b919ec0874b7a1c74dd8f6d0377cb709a83dca3105975227e9f9339db740b9c`）

## 4. 限制与边界说明

1. **客户端未签名**：私有试用客户端未做数字签名，验收基于 NSIS 默认的按用户（per-user）安装路径；MSI 包仅作为候选产物记录。
2. **后置依赖服务**：Exchange 与 Persistence 服务须预先部署就绪；客户端安装包不包含也不部署后端、Reactor、vLLM 或 GPU 驱动。
3. **远端 CI 计费限制**：GitHub Actions 因外部 spending limit 处于 0 steps，明确标记为 `BLOCKED_EXTERNAL`；不使用旧提交的绿灯结果替代新构建包的验收。
4. **测试隐私保护**：归档在 `proof/` 的所有收据均已脱敏，排除了数据库连接串、未哈希 token、账号凭据和完整原始对话内容。
