# Cyrene Developer Quickstart

## 1. Prerequisites
- **Operating System**: Linux (Ubuntu 22.04+ recommended) or macOS / Windows with WSL2
- **Runtimes**:
  - Python 3.12+ (managed via `uv`)
  - .NET SDK 10.0 LTS
  - Rust 1.80+ (managed via `rustup`)
  - Git 2.40+ and GitHub CLI (`gh`)
  - Azure DevOps CLI (`az devops`)

## 2. Workspace Initialization

```bash
# Clone the meta-workspace
git clone https://github.com/DoHorizon-AI/Cyrene-Workspace.git
cd Cyrene-Workspace

# Run bootstrap to clone and link sibling repositories
./bootstrap.sh
```

## 3. Verifying the System
Run verification to validate environment integrity:

```bash
# Verify Python virtual environments and dependencies
uv sync

# Run governance guard across repositories
bash Cyrene-Platform/tooling/ci/check-no-legacy-surface.sh .
```

## 4. Running a Local Service
To start the inference serving backend:

```bash
cd Cyrene-Services/Cyrene-Reactor
cargo check
uv run pytest
```

## 5. Backing Up and Restoring Local Data

The unified CLI writes backups atomically and requires the destination to be
outside `CYRENE_DEV_HOME` (by default `~/.local/state/cyrene/dev`):

```bash
./cyrene backup --dest ~/backups/
./cyrene restore --src ~/backups/cyrene-backup-YYYYMMDD-HHMMSS.tar.gz
```

Restore accepts a CLI-created archive with one `cyrene-dev` root, or an
unrelated directory. It stops active services, validates and stages the full
backup, then atomically replaces the data directory. An invalid or incomplete
backup leaves the existing directory in place.

To start the API Gateway:

```bash
cd Cyrene-Services/Cyrene-Exchange
uv run pytest
```
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 开发者快速入门

## 1. 前置条件
- **操作系统**：Linux（推荐 Ubuntu 22.04 或更高版本），或通过 WSL2 使用 macOS / Windows。
- **运行时**：
  - Python 3.12+（通过 `uv` 管理）
  - .NET SDK 10.0 LTS
  - Rust 1.80+（通过 `rustup` 管理）
  - Git 2.40+ 和 GitHub CLI（`gh`）
  - Azure DevOps CLI（`az devops`）

## 2. 初始化工作区

```bash
# 克隆元工作区
git clone https://github.com/DoHorizon-AI/Cyrene-Workspace.git
cd Cyrene-Workspace

# 运行引导脚本以克隆并链接同级仓库
./bootstrap.sh
```

## 3. 验证系统
运行验证以检查环境完整性：

```bash
# 验证 Python 虚拟环境及依赖
uv sync

# 在各仓库运行治理守卫
bash Cyrene-Platform/tooling/ci/check-no-legacy-surface.sh .
```

## 4. 启动本地服务
如需启动推理服务后端：

```bash
cd Cyrene-Services/Cyrene-Reactor
cargo check
uv run pytest
```

## 5. 备份与恢复本地数据

统一 CLI 以原子方式写入备份，并要求备份目标位于 `CYRENE_DEV_HOME` 之外（默认值为 `~/.local/state/cyrene/dev`）：

```bash
./cyrene backup --dest ~/backups/
./cyrene restore --src ~/backups/cyrene-backup-YYYYMMDD-HHMMSS.tar.gz
```

恢复操作接受由 CLI 创建、且包含一个 `cyrene-dev` 根目录的归档，也接受无关目录。恢复前会停止活动服务，校验并暂存完整备份，然后以原子方式替换数据目录。若备份无效或不完整，现有目录会保留。

如需启动 API 网关：

```bash
cd Cyrene-Services/Cyrene-Exchange
uv run pytest
```

## 5. Backing Up and Restoring Local Data

The unified CLI writes backups atomically and requires the destination to be
outside `CYRENE_DEV_HOME` (by default `~/.local/state/cyrene/dev`):

```bash
./cyrene backup --dest ~/backups/
./cyrene restore --src ~/backups/cyrene-backup-YYYYMMDD-HHMMSS.tar.gz
```

Restore accepts a CLI-created archive with one `cyrene-dev` root, or an
unrelated directory. It stops active services, validates and stages the full
backup, then atomically replaces the data directory. An invalid or incomplete
backup leaves the existing directory in place.

To start the API Gateway:

```bash
cd Cyrene-Services/Cyrene-Exchange
uv run pytest
```
