# Cyrene Navigator V8 Source and Version Matrix / 源码与版本矩阵

> Historical package snapshot / 历史交付快照：这些 SHA 只证明 V8 Alpha 安装包的
> 可复现来源，不代表当前分支、当前权威或推荐的 Platform–Plugins 调用路径。

This document records the authoritative source commits, upstream pins, binary digests,
and dependencies for the Cyrene Navigator V8 Phase 0 handoff.

本文档记录 Cyrene Navigator V8 Phase 0 交接的权威源码提交、上游版本、二进制校验和及依赖。

---

## 1. Multi-Repository Source Commits / 多仓库源码提交

All repositories are evaluated on their task branches; no local acceptance is inferred as canonical mainline merge.
所有仓库均基于任务分支评估；本地验收不直接等同于主线正式合并。

| Repository / 仓库 | Path / 路径 | Branch / 分支 | Exact Full Commit SHA / 精确完整提交 | Delivery & PR Status / 交付与 PR 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **Cyrene-Navigator** | `Cyrene-Services/Cyrene-Navigator` | `feat/text-model-lifecycle-v1` | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` | Pushed; no open PR; working tree clean |
| **Cyrene-Workspace** | `Cyrene-Workspace` | `feat/text-model-lifecycle-v1` | `77b313b91a0d4f0cceb4b69f3a74e37bf65e7a91` | Pushed; [Draft PR #4](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/4) → `main` |
| **Cyrene-Platform** (CES & Resolver) | `Cyrene-Platform` | `feat/text-model-lifecycle-v1` | `30272145b9c11df9948465359479c3d95d08a1dc` | Pushed; [Draft PR #31](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/31) stacked on #30 |
| **Cyrene-Platform** (cy-manifest) | `Cyrene-Platform` | `feat/text-model-lifecycle-v1` | `185527f82c83700d4f567ac563a393a2e1c18c87` | Bundled in V8 package via static CRT |
| **Cyrene-Exchange** | `Cyrene-Services/Cyrene-Exchange` | `feat/text-model-lifecycle-v1` | `91c509904add26de941e2e14bc3b2b4752233d28` | Pushed; [Draft PR #7](https://github.com/DoHorizon-AI/Cyrene-Exchange/pull/7) → `develop` |
| **Cyrene-Plugins-Official** | `Cyrene-Plugins-Official` | `feat/text-model-lifecycle-v1` | `261a78fd36c7cb2da85a504a89ec7c9c353ecc19` | Pushed; [Draft PR #9](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/pull/9) → `develop` |

---

## 2. Detailed Platform Roles and Commit Breakdown / Platform 提交用途与角色划分

Platform 仓库在本次交付中承担不同角色的组件，具体提交与用途如下：

1. **后端能力解析与执行守护进程 (Resolver & CES)**:
   - **Full Commit SHA**: `30272145b9c11df9948465359479c3d95d08a1dc`
   - **组件用途**: 构建运行在宿主机/服务端的 `cyrene-capability-resolver` 与 `cyrene-capability-execution-service` 二进制守护进程，负责模型工具能力调度与分发。
   - **实测二进制 SHA-256**:
     - `ces`: `923480ad3a88d3f37038b1d7ea462f43377b74a056199956040325fc1f1ddfa7`
     - `resolver`: `bff45fd4f589fcca62502ed1123817c9e47fa38d80e57c04e773b5ada4764ac0`
2. **随包原生工具二进制 (Bundled cy-manifest.exe)**:
   - **Full Commit SHA**: `185527f82c83700d4f567ac563a393a2e1c18c87`
   - **组件用途**: 静态嵌入在 Windows V8 桌面客户端安装包中的原生工具 `cy-manifest.exe`。
   - **采用理由**: 采用静态 MSVC 运行时（`/MT`）构建，彻底剥离对干净 Windows VM 缺少 `VCRUNTIME140.dll` 的外部环境依赖，确保开箱即用。
3. **SDK 依赖 (Python Capability Client SDK)**:
   - **Full Commit SHA**: `30272145b9c11df9948465359479c3d95d08a1dc`
   - **组件用途**: Platform Python SDK，由 Exchange Product 在 `product/pyproject.toml` 中作为 editable 路径引用消费。
4. **覆盖验证收据 (Composition Evidence)**:
   - **收据文件**: `proof/windows-v8-frozen-backends-tool-audit.json`
   - **收据 SHA-256**: `48a202df3f48a17beec4b80261175eb82bcebc6d628c5fe1190539ecb71a06a5`
   - **验证事实**: 证明该组合下，已安装客户端通过随包 `cy-manifest.exe` 执行工具，由 Platform `30272145...` 后端提供调度并与 Exchange 联动完成流式续答，完整跑通 77→90 events。

---

## 3. Upstream Core Pin / 上游核心固定

- **Upstream Repository**: `deepseek-ai/deepseek-harness`
- **Release Tag**: `dsh-v0.1.3-alpha.1`
- **Exact Full Commit**: `d347e703908d0406b7a7ef80e3a0e594d86b2215`
- **Core Modifications**: `UPSTREAM_CORE_PATCHES = 0` (Zero upstream patches; all Cyrene integrations operate through explicit Profile, Bundle, adapter, and bridge layers).

---

## 4. Package and Binary Checksums / 安装包与二进制校验和

| Artifact / 产物 | Filename / 文件名 | Size / 大小 | SHA-256 Checksum / SHA-256 校验和 |
| :--- | :--- | :--- | :--- |
| **Accepted NSIS Installer** | `Cyrene Navigator_0.1.0_x64-setup.exe` | 52,603,218 bytes | `792d21adbbc2b95945dd61bfe43c68097a3d93d1f987dcb6e1cbc07687943a90` |
| **Installed Executable** | `Cyrene Navigator.exe` | - | `0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1` |
| **Candidate MSI Installer** | `Cyrene Navigator_0.1.0_x64_en-US.msi` | 99,378,356 bytes | `f825b6b569c8eefc8650fbc7b731cd5b24265ca86bd661f1b7b65565d38a52e1` |
| **Package Metadata Receipt** | `windows-v8-package.json` | 1,261 bytes | `850e17fa78136c52a9a73d71f282ef7fb5e09e5e94709d97d92821456b0e44fc` |
| **Running CES Binary** | `cyrene-capability-execution-service` | - | `923480ad3a88d3f37038b1d7ea462f43377b74a056199956040325fc1f1ddfa7` |
| **Running Resolver Binary** | `cyrene-capability-resolver` | - | `bff45fd4f589fcca62502ed1123817c9e47fa38d80e57c04e773b5ada4764ac0` |

---

## 5. Runtime Tree Parity / 运行时完整度校验

- **Total Shipped Files**: 24,382 regular files
- **Stable Content Map SHA-256**: `1b2fd08a483fc050e6cd0e6cdfd477220d39f06f10f0b085899ec9505fe096a3`
- **Comparison Receipt**: `proof/windows-v8-complete-runtime-parity.json` (SHA-256: `6b919ec0874b7a1c74dd8f6d0377cb709a83dca3105975227e9f9339db740b9c`)
- **Parity Result**: Zero missing, zero added, zero mismatched files between staging directory and installed guest runtime.

---

## 6. Environment & Toolchains / 环境与构建工具链

- **Host OS**: Linux WSL2 x86_64 (NVIDIA GeForce RTX 5070 12GB VRAM, Driver `616.56`)
- **Guest OS**: Clean Windows 11 64-bit isolated VM (no preinstalled Node, Python, or MSVC runtimes)
- **Rust Toolchain**: `1.96.1` / `1.97.1` (Cargo locked workspace, clippy zero warnings)
- **Python Runtime**: Managed CPython `3.12.11` (uv locked dependencies)
- **Node & Package Manager**: Node `24.13.0`, pnpm `11.7.0` (frozen lockfile)
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene Navigator V8 源码与版本矩阵

> 历史软件包快照：这些 SHA 只证明 V8 Alpha 安装包的可复现来源，不代表当前分支、当前权威或推荐的 Platform–Plugins 调用路径。

本文记录 Cyrene Navigator V8 Phase 0 交接所使用的权威源码提交、上游固定版本、二进制摘要和依赖。

## 1. 多仓库源码提交

所有仓库都基于各自任务分支评估；本地验收不能推断为规范主线合并。

| 仓库 | 路径 | 分支 | 精确完整提交 SHA | 交付与 PR 状态 |
|---|---|---|---|---|
| **Cyrene-Navigator** | `Cyrene-Services/Cyrene-Navigator` | `feat/text-model-lifecycle-v1` | `a297d1cae54c5fbb8bffa68d748c59a9e7c1aabe` | 已推送；没有开放 PR；工作树干净 |
| **Cyrene-Workspace** | `Cyrene-Workspace` | `feat/text-model-lifecycle-v1` | `77b313b91a0d4f0cceb4b69f3a74e37bf65e7a91` | 已推送；[Draft PR #4](https://github.com/DoHorizon-AI/Cyrene-Workspace/pull/4) → `main` |
| **Cyrene-Platform**（CES 与 Resolver）| `Cyrene-Platform` | `feat/text-model-lifecycle-v1` | `30272145b9c11df9948465359479c3d95d08a1dc` | 已推送；[Draft PR #31](https://github.com/DoHorizon-AI/Cyrene-Platform/pull/31) 依赖 #30 |
| **Cyrene-Platform**（cy-manifest）| `Cyrene-Platform` | `feat/text-model-lifecycle-v1` | `185527f82c83700d4f567ac563a393a2e1c18c87` | 通过静态 CRT 打包进 V8 软件包 |
| **Cyrene-Exchange** | `Cyrene-Services/Cyrene-Exchange` | `feat/text-model-lifecycle-v1` | `91c509904add26de941e2e14bc3b2b4752233d28` | 已推送；[Draft PR #7](https://github.com/DoHorizon-AI/Cyrene-Exchange/pull/7) → `develop` |
| **Cyrene-Plugins-Official** | `Cyrene-Plugins-Official` | `feat/text-model-lifecycle-v1` | `261a78fd36c7cb2da85a504a89ec7c9c353ecc19` | 已推送；[Draft PR #9](https://github.com/DoHorizon-AI/Cyrene-Plugins-Official/pull/9) → `develop` |

## 2. Platform 详细角色与提交划分

Platform 仓库在本次交付中提供承担不同职责的组件；提交与用途如下：

1. **后端能力解析与执行守护进程（Resolver 与 CES）**：
   - **完整提交 SHA**：`30272145b9c11df9948465359479c3d95d08a1dc`
   - **组件用途**：构建运行于宿主机 / 服务端的 `cyrene-capability-resolver` 与 `cyrene-capability-execution-service` 二进制守护进程，负责模型工具能力的调度与分发。
   - **实测二进制 SHA-256**：
     - `ces`：`923480ad3a88d3f37038b1d7ea462f43377b74a056199956040325fc1f1ddfa7`
     - `resolver`：`bff45fd4f589fcca62502ed1123817c9e47fa38d80e57c04e773b5ada4764ac0`
2. **随包原生工具二进制（Bundled `cy-manifest.exe`）**：
   - **完整提交 SHA**：`185527f82c83700d4f567ac563a393a2e1c18c87`
   - **组件用途**：原生工具 `cy-manifest.exe`，静态嵌入 Windows V8 桌面客户端安装包。
   - **采用理由**：使用静态 MSVC 运行时（`/MT`）构建，从而彻底移除对干净 Windows VM 中缺少 `VCRUNTIME140.dll` 的外部环境依赖，确保安装后即可使用。
3. **SDK 依赖（Python Capability Client SDK）**：
   - **完整提交 SHA**：`30272145b9c11df9948465359479c3d95d08a1dc`
   - **组件用途**：Platform Python SDK，由 Exchange Product 在 `product/pyproject.toml` 中以 editable 路径引用。
4. **覆盖验证收据（Composition Evidence）**：
   - **收据文件**：`proof/windows-v8-frozen-backends-tool-audit.json`
   - **收据 SHA-256**：`48a202df3f48a17beec4b80261175eb82bcebc6d628c5fe1190539ecb71a06a5`
   - **验证事实**：证明在此组合中，已安装客户端通过随包 `cy-manifest.exe` 执行工具；Platform `30272145...` 后端负责调度，并与 Exchange 协作完成流式续答，完整运行 77→90 个 events。

## 3. 上游核心版本固定

- **上游仓库**：`deepseek-ai/deepseek-harness`
- **Release Tag**：`dsh-v0.1.3-alpha.1`
- **精确完整提交**：`d347e703908d0406b7a7ef80e3a0e594d86b2215`
- **核心修改**：`UPSTREAM_CORE_PATCHES = 0`（没有任何上游补丁；所有 Cyrene 集成都通过显式 Profile、Bundle、adapter 和 bridge 层完成。）

## 4. 软件包与二进制校验和

| 制品 | 文件名 | 大小 | SHA-256 校验和 |
|---|---|---|---|
| **已接受的 NSIS 安装程序** | `Cyrene Navigator_0.1.0_x64-setup.exe` | 52,603,218 bytes | `792d21adbbc2b95945dd61bfe43c68097a3d93d1f987dcb6e1cbc07687943a90` |
| **已安装的可执行文件** | `Cyrene Navigator.exe` | - | `0acf4412a424c91139dcc23fd943491fb168f5a932f6a34e902bb6618f19bcd1` |
| **候选 MSI 安装程序** | `Cyrene Navigator_0.1.0_x64_en-US.msi` | 99,378,356 bytes | `f825b6b569c8eefc8650fbc7b731cd5b24265ca86bd661f1b7b65565d38a52e1` |
| **包元数据收据** | `windows-v8-package.json` | 1,261 bytes | `850e17fa78136c52a9a73d71f282ef7fb5e09e5e94709d97d92821456b0e44fc` |
| **运行中的 CES 二进制文件** | `cyrene-capability-execution-service` | - | `923480ad3a88d3f37038b1d7ea462f43377b74a056199956040325fc1f1ddfa7` |
| **运行中的 Resolver 二进制文件** | `cyrene-capability-resolver` | - | `bff45fd4f589fcca62502ed1123817c9e47fa38d80e57c04e773b5ada4764ac0` |

## 5. 运行时目录一致性

- **随包文件总数**：24,382 个普通文件
- **稳定内容映射 SHA-256**：`1b2fd08a483fc050e6cd0e6cdfd477220d39f06f10f0b085899ec9505fe096a3`
- **比对收据**：`proof/windows-v8-complete-runtime-parity.json`（SHA-256：`6b919ec0874b7a1c74dd8f6d0377cb709a83dca3105975227e9f9339db740b9c`）
- **一致性结果**：暂存目录和已安装 guest runtime 之间没有缺失、新增或不匹配文件。

## 6. 环境与工具链

- **Host OS**：Linux WSL2 x86_64（NVIDIA GeForce RTX 5070，12GB VRAM，驱动 `616.56`）
- **Guest OS**：干净、隔离的 Windows 11 64 位虚拟机（未预装 Node、Python 或 MSVC 运行时）
- **Rust 工具链**：`1.96.1` / `1.97.1`（Cargo 锁定工作区，clippy 无警告）
- **Python 运行时**：托管 CPython `3.12.11`（通过 uv 锁定依赖）
- **Node 与包管理器**：Node `24.13.0`、pnpm `11.7.0`（冻结 lockfile）
