# Cyrene Legacy Surface Migration Closeout Report

## 1. Executive Summary
The Cyrene Legacy Surface Final Closure initiative systematically identified, quarantined, and eliminated historical legacy code, shims, dead files, and unverified data from the Cyrene codebase.

All repositories now enforce the clean-room invariant via continuous integration guards:
- **`UNEXPECTED_LEGACY = 0`**
- **Canonical Guard Script**: `check-no-legacy-surface.sh` in `Cyrene-Platform` (pinned across all Azure Pipelines).

## 2. Inventory of Executed Actions

### Cyrene-Platform (PR #36)
- Deleted historical `legacy/`, `archive/`, and dead compatibility shims.
- Implemented `tooling/ci/check-no-legacy-surface.sh` with exit code 0 on clean tree.
- Merged to `origin/develop` at commit `a402b7b8efd3ffe279d052e1f2c58148fd85d8d2`.

### Cyrene-Plugins-Official (PR #10)
- Deleted 16 dead legacy files: `plugin.legacy.toml` and `LEGACY_PLUGIN.md` from active plugins (`docker-uv-builder`, `gateway/python`, `hf-model-analyzer`, `compat-rules`, `seedance-video-gen`), `legacy-requirements/`, and `legacy-config/`.
- Preserved operator-approved `plugins/**/compatibility/` directories.
- Implemented governance conformance test `test_no_legacy_surface.py` (all tests passing).

### Cyrene-Catalyst (PR #6)
- Relocated full dialogue corpus to external storage (`../DH-LLMs/genshin-dialogue-zh-v2025-11-20/`).
- Curated minimal representative public sample in `data/samples/genshin-dialogue-zh/` with strict provenance and license documentation.
- Integrated platform guard into Azure Pipeline.

### Cyrene-Echo (PR #6)
- Removed deleted `legacy/navigator-feedback` from `service.json`.
- Integrated platform guard into Azure Pipeline.
- Locked clean-room test suite.

### Cyrene-Reactor (PR #11), Cyrene-Navigator (PR #5), Cyrene-Exchange (PR #12)
- Cleaned legacy source trees and pinned platform guard to canonical commit.

### AstrBot-Rev (retired)
- The AstrBot-Rev repository was retired; its migrated code now lives under `Cyrene-Plugins-Official/plugins/`.
---
<!-- Chinese Translation / 中文翻译 -->

# Cyrene 旧版代码面的迁移收尾报告

## 1. 执行摘要
Cyrene 旧版代码面最终清理计划系统性地识别、隔离并移除了代码库中的历史旧版代码、兼容垫片、无用文件和未经核实的数据。

现在所有仓库都通过持续集成守卫执行洁净室不变量：
- **`UNEXPECTED_LEGACY = 0`**
- **规范守卫脚本**：`Cyrene-Platform` 中的 `check-no-legacy-surface.sh`（已固定版本并接入所有 Azure Pipelines）。

## 2. 已执行操作清单

### Cyrene-Platform（PR #36）
- 删除历史 `legacy/`、`archive/` 目录和无用兼容垫片。
- 实现 `tooling/ci/check-no-legacy-surface.sh`；在干净代码树上退出码为 0。
- 已于提交 `a402b7b8efd3ffe279d052e1f2c58148fd85d8d2` 合并至 `origin/develop`。

### Cyrene-Plugins-Official（PR #10）
- 删除 16 个无用旧版文件：活动插件（`docker-uv-builder`、`gateway/python`、`hf-model-analyzer`、`compat-rules`、`seedance-video-gen`）中的 `plugin.legacy.toml` 与 `LEGACY_PLUGIN.md`，以及 `legacy-requirements/` 和 `legacy-config/`。
- 保留运营方批准的 `plugins/**/compatibility/` 目录。
- 实现治理一致性测试 `test_no_legacy_surface.py`（所有测试通过）。

### Cyrene-Catalyst（PR #6）
- 将完整对话语料迁移到外部存储（`../DH-LLMs/genshin-dialogue-zh-v2025-11-20/`）。
- 在 `data/samples/genshin-dialogue-zh/` 中整理出带有严格来源与许可说明的最小代表性公开样本。
- 将 Platform 守卫接入 Azure Pipeline。

### Cyrene-Echo（PR #6）
- 从 `service.json` 中移除已删除的 `legacy/navigator-feedback`。
- 将 Platform 守卫接入 Azure Pipeline。
- 锁定洁净室测试套件。

### Cyrene-Reactor（PR #11）、Cyrene-Navigator（PR #5）、Cyrene-Exchange（PR #12）
- 清理旧版源码树，并将 Platform 守卫固定到规范提交。

### AstrBot-Rev（已退役）
- AstrBot-Rev 仓库已退役；迁移后的代码现位于 `Cyrene-Plugins-Official/plugins/`。
