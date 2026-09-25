# Cyrene Developer Workflow & Engineering Guide

## 1. Multi-Repository Topology
The Cyrene ecosystem is orchestrated via `Cyrene-Workspace`.
- The repository topology is defined in [`repositories.yaml`](../repositories.yaml).
- Repositories are checked out as sibling directories under their designated paths (`Services/*`, `Cyrene-Platform`, `Cyrene-Plugins-Official`).
- Every repository is independently buildable and testable.

## 2. Agent Worktree Isolation
To prevent race conditions during concurrent multi-agent or multi-developer workflows, git worktree isolation is mandatory:

```bash
# Create dedicated task worktree for an agent
./agent-worktree.sh create -Repo plugins -Branch chore/plugin-modernization -Base origin/develop -Role dev-plugins

# Check worktree status
./agent-worktree.sh status

# Remove worktree after merge
./agent-worktree.sh remove -Role dev-plugins
```

## 3. IDE First Development
- **JetBrains IntelliJ IDEA / PyCharm / CLion**: Open `Cyrene-Workspace` root. Native project modules are pre-configured.
- **JetBrains Rider / Visual Studio**: Open `Cyrene.Workspace.slnx` for .NET solutions.
- Code style and commenting must follow the `dev-suite` and `code-style` standards.

## 4. Local Quality Gates Before Push
Always run local quality checks before pushing branches:
```bash
# Platform checks
cd Cyrene-Platform
bash tooling/ci/check-no-legacy-surface.sh .

# Plugins checks
cd Cyrene-Plugins-Official
uv run pytest conformance/tests/test_no_legacy_surface.py
```
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 开发者工作流与工程指南

## 1. 多仓库拓扑
Cyrene 生态由 `Cyrene-Workspace` 统一编排。
- 仓库拓扑定义在 [`repositories.yaml`](../repositories.yaml) 中。
- 各仓库按指定路径检出为同级目录（`Services/*`、`Cyrene-Platform`、`Cyrene-Plugins-Official`）。
- 每个仓库都可以独立构建和测试。

## 2. Agent 工作树隔离
为避免多个 Agent 或开发者并行工作时发生竞态，必须使用 Git 工作树隔离：

```bash
# 为 Agent 创建专用任务工作树
./agent-worktree.sh create -Repo plugins -Branch chore/plugin-modernization -Base origin/develop -Role dev-plugins

# 查看工作树状态
./agent-worktree.sh status

# 合并后移除工作树
./agent-worktree.sh remove -Role dev-plugins
```

## 3. IDE 优先开发
- **JetBrains IntelliJ IDEA / PyCharm / CLion**：打开 `Cyrene-Workspace` 根目录。原生项目模块已预先配置。
- **JetBrains Rider / Visual Studio**：打开 .NET 解决方案 `Cyrene.Workspace.slnx`。
- 代码风格和注释必须遵循 `dev-suite` 与 `code-style` 标准。

## 4. 推送前的本地质量门禁
推送分支前务必运行本地质量检查：

```bash
# Platform 检查
cd Cyrene-Platform
bash tooling/ci/check-no-legacy-surface.sh .

# Plugins 检查
cd Cyrene-Plugins-Official
uv run pytest conformance/tests/test_no_legacy_surface.py
```
