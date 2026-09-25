# Branch Consolidation Execution Plan — 2026-09-10

> **Purpose / 目的**: Land all recent in-flight decoupling branches into their integration
> branch (`develop`; `Cyrene-Workspace` uses `main`), push, validate on Azure Pipelines, then
> promote `develop → main` via PR. This document is a **resumable checkpoint**: if the executing
> agent runs out of quota, another agent (or human) can continue from the Progress table below.

## 0. Ground rules (decided by owner)

1. **Scope**: Do group **A** to completion. Group **B** is best-effort (may not finish).
   Group **C** must **not** be touched.
2. **Merge locally first**, then **push all merged branches to remote `develop` together**.
3. After all pushes, **run the Azure Pipeline tests**. If green, open a **`develop → main` PR**
   per repository, then stop.
4. **Step 3 (branch cleanup) is PAUSED.** Do **NOT** delete any branch (local or remote).
   The owner will review and delete manually.

## 1. Environment facts (verified)

- All 9 checkouts under `/home/baijin/Dev/Cyrene` are **clean** and local base == `origin` base.
- `gh` authenticated as `Baijin64` (scopes: `repo`, `workflow`, `read:org`, `gist`).
- `az` CLI 2.87.0, `az devops` defaults → org `https://dev.azure.com/dohorizon`, project `Cyrene`.
- Azure pipeline definitions (one per repo): `Cyrene-Platform`, `Cyrene-Plugins-Official`,
  `Cyrene-Reactor`, `Cyrene-Yield`, `Cyrene-Exchange`, `Cyrene-Catalyst`, `Cyrene-Echo`,
  `Cyrene-Navigator`, `Cyrene-Workspace`.
- `develop` has no enforced branch protection for these private repos on the current plan →
  direct push is accepted (still push **only after** all local merges succeed).
- Azure `trigger` includes `develop`, so a push auto-triggers; manual run:
  `az pipelines run --name <repo> --branch develop --project Cyrene --organization https://dev.azure.com/dohorizon`.

Repo paths (relative to `/home/baijin/Dev/Cyrene`):
`Cyrene-Platform`, `Cyrene-Plugins-Official`, `Cyrene-Services/Cyrene-{Reactor,Yield,Exchange,Catalyst,Echo,Navigator}`, `Cyrene-Workspace`.

## 2. Group A — merge set (do to completion)

Merge each branch into its integration branch. Recommended command per branch:

```bash
git -C <repo> checkout <base>
git -C <repo> merge --no-ff --no-edit <branch>
```

Snapshot SHAs at plan time (2026-09-10):

| Repo | base @ sha | Branch | tip sha | + |
| :--- | :--- | :--- | :--- | ---: |
| Cyrene-Platform | develop `dc37791` | chore/ci-authority-boundary-20260910 | `287124ed8500` | 1 |
| Cyrene-Platform | | chore/legacy-surface-astrbot-20260910 | `6025845caa5f` | 1 |
| Cyrene-Plugins-Official | develop `91f5e1c` | fix/spring-gateway-daemon-jvm | `c8052102918a` | 1 |
| Cyrene-Plugins-Official | | feat/conversation-store-v1 | `63f045010f4e` | 4 |
| Cyrene-Plugins-Official | | feat/exchange-spring-plugin-core-v1 | `6b3fdebeffdb` | 7 |
| Cyrene-Plugins-Official | | refactor/astrbot-capability-completion | `a6a2f6eec32d` | 21 |
| Cyrene-Reactor | develop `a056148` | chore/ci-authority-boundary-20260910 | `4322875228e7` | 1 |
| Cyrene-Reactor | | refactor/remove-hybrid-fallback-20260909 | `b444da487e03` | 1 |
| Cyrene-Reactor | | fix/remove-simulated-lora-swapper-20260909 | `d4eadc29f065` | 2 |
| Cyrene-Reactor | | refactor/remove-dead-sidecar-telemetry-20260909 | `ce117b633578` | 2 |
| Cyrene-Reactor | | refactor/remove-fallback-scheduler-bridge-20260909 | `29a4faaa5fd3` | 1 |
| Cyrene-Yield | develop `6931c2d9` | chore/ci-authority-boundary-20260910 | `2f2c86afd52c` | 1 |
| Cyrene-Exchange | develop `2bc5dfa` | chore/ci-authority-boundary-20260910 | `3734fb7371b6` | 1 |
| Cyrene-Exchange | | refactor/extract-spring-plugin-services-v1 | `4f8ea210a1af` | 5 |
| Cyrene-Catalyst | develop `35b7d3e` | chore/ci-authority-boundary-20260910 | `e64394a8a2cb` | 1 |
| Cyrene-Echo | develop `128b051` | chore/ci-authority-boundary-20260910 | `622667507657` | 1 |
| Cyrene-Navigator | develop `f83a701` | chore/ci-authority-boundary-20260910 | `c74d5a9d6925` | 1 |
| Cyrene-Workspace | main `950dc4b` | chore/remove-retired-workspace-config | `f72d3ac9a891` | 5 |
| Cyrene-Workspace | | chore/ci-authority-boundary-20260910 | `d539d51cc930` | 1 |
| Cyrene-Workspace | | chore/text-lifecycle-phase0-20260910 | `fbc5701bda8e` | 1 |
| Cyrene-Workspace | | fix/database-authority-truth-20260909 | `06aa5f769cfa` | 1 |
| Cyrene-Workspace | | fix/dh-integration-authority-20260909 | `78f3bd2edcb4` | 1 |

> `chore/ci-authority-boundary-20260910` is one branch per repo (9 total) — it is **not** the same
> branch object across repos; merge the copy that exists in each repo.

All Group-A merges were conflict-dry-run at plan time using
`git merge-tree --write-tree --name-only origin/<base> <branch>` → **CLEAN**.
Re-check for conflicts after each merge if a later branch touches the same files.

## 3. Group B — conflicted / best-effort (may remain unfinished)

Dry-run shows conflicts; **do not merge blindly**. Assess "already superseded by develop?" first.

| Repo | Branch | ahead / conflicts |
| :--- | :--- | :--- |
| Cyrene-Plugins-Official | chore/plugins-ci-boundary-20260910 | 12 / 166 |
| Cyrene-Plugins-Official | feat/unified-official-plugins | 9 / 141 |
| Cyrene-Platform | feat/training-capability-client-v1 | 1 / 10 |
| Cyrene-Yield | ci/azure-ci-baseline | 7 / 5 |
| Cyrene-Navigator | docs/navigator-bilingual-documentation | 1 / 5 |

Possible resolution path: `git checkout <base> && git merge <branch>`; if it resolves trivially
(`git diff --diff-filter=U --quiet`), commit; otherwise `git merge --abort` and record "manual".

## 4. Group C — DO NOT TOUCH

- `Cyrene-Yield` `archive/icy-lunar-llamafactory` (ahead 3090 — archive branch).
- All `dependabot/*` branches (5 total) — handled via GitHub PR review.
- Per-repo `main` and stray local `origin` branches (release / accidental duplicates).
- Stale 2026-08-26 … 2026-08-31 branches (e.g. Yield `feat/artifact-plane-mvp-current`,
  `docs/*-bilingual-documentation` dated 08-31).

## 5. Push + validate + promote

1. **Push** (only after every Group-A local merge is done):
   `git -C <repo> push origin <base>` for each repo that changed.
2. **Azure**: confirm a run per repo. Manual trigger if needed:
   `az pipelines run --name <repo> --branch develop --project Cyrene --organization https://dev.azure.com/dohorizon`
   Then poll `az pipelines runs list --pipeline-ids <id> ...` / `gh` as needed.
3. If all green → open **`develop → main` PR per repo** (`gh pr create --base main --head develop ...`).
4. **Stop.** Do not delete branches (owner handles cleanup).

## 6. Progress table (update after each step)

| Repo | A merged | pushed | Azure | dev→main PR |
| :--- | :---: | :---: | :---: | :---: |
| Cyrene-Platform | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Plugins-Official | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Reactor | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Yield | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Exchange | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Catalyst | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Echo | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Navigator | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Workspace | [ ] | [ ] | [ ] | [ ] |

## 7. Resume notes for the next agent

- If interrupted mid-merge in a repo: run `git -C <repo> status`; if a merge is in progress,
  finish it or `git merge --abort`, then continue with the remaining Group-A branches.
- Verify idempotently before merging: `git -C <repo> merge-base --is-ancestor <branch> origin/<base>`
  → if true, that branch is already landed, skip it.
- Never `push --force`. Never delete branches. Never touch Group C.
---
<!-- Chinese Translation / 中文翻译 -->

# 分支整合执行计划 — 2026-09-10

> **目的**：将近期所有正在进行的解耦分支合入各自集成分支（`develop`；`Cyrene-Workspace` 使用 `main`），推送、在 Azure Pipelines 上验证，然后通过 PR 将 `develop` 提升到 `main`。本文是一个**可恢复检查点**：如果执行中的 Agent 用尽额度，其他 Agent 或人员可以从下方进度表继续。

## 0. 基本规则（由 owner 决定）

1. **范围**：完整完成 A 组；尽力处理 B 组（可以未完成）；**不得**触碰 C 组。
2. **先在本地合并**，然后将所有已合并分支一起推送到远端 `develop`。
3. 全部推送完成后，**运行 Azure Pipeline 测试**。若全部通过，按仓库分别创建 **`develop → main` PR**，然后停止。
4. **步骤 3（清理分支）已暂停。**不得删除任何本地或远端分支；owner 会审查并手动删除。

## 1. 环境事实（已验证）

- `/home/baijin/Dev/Cyrene` 下的 9 个检出均**干净**，且本地基线与 `origin` 基线一致。
- `gh` 已以 `Baijin64` 身份认证（scope：`repo`、`workflow`、`read:org`、`gist`）。
- Azure CLI 2.87.0；`az devops` 默认指向组织 `https://dev.azure.com/dohorizon` 和项目 `Cyrene`。
- 每个仓库各有一个 Azure Pipeline 定义：`Cyrene-Platform`、`Cyrene-Plugins-Official`、`Cyrene-Reactor`、`Cyrene-Yield`、`Cyrene-Exchange`、`Cyrene-Catalyst`、`Cyrene-Echo`、`Cyrene-Navigator`、`Cyrene-Workspace`。
- 按当前计划，这些私有仓库的 `develop` 没有强制分支保护，因此允许直接推送（但仍须**等所有本地合并成功之后再推送**）。
- Azure `trigger` 包含 `develop`，因此推送会自动触发。手动运行命令：`az pipelines run --name <repo> --branch develop --project Cyrene --organization https://dev.azure.com/dohorizon`。

仓库路径（相对于 `/home/baijin/Dev/Cyrene`）：`Cyrene-Platform`、`Cyrene-Plugins-Official`、`Cyrene-Services/Cyrene-{Reactor,Yield,Exchange,Catalyst,Echo,Navigator}`、`Cyrene-Workspace`。

## 2. A 组 — 合并集合（全部完成）

将每个分支合入其集成分支。每个分支建议运行：

```bash
git -C <repo> checkout <base>
git -C <repo> merge --no-ff --no-edit <branch>
```

计划编写时的 SHA 快照（2026-09-10）：

| 仓库 | 基线 @ SHA | 分支 | 末端 SHA | + |
|---|---|---|---|---:|
| Cyrene-Platform | develop `dc37791` | chore/ci-authority-boundary-20260910 | `287124ed8500` | 1 |
| Cyrene-Platform | | chore/legacy-surface-astrbot-20260910 | `6025845caa5f` | 1 |
| Cyrene-Plugins-Official | develop `91f5e1c` | fix/spring-gateway-daemon-jvm | `c8052102918a` | 1 |
| Cyrene-Plugins-Official | | feat/conversation-store-v1 | `63f045010f4e` | 4 |
| Cyrene-Plugins-Official | | feat/exchange-spring-plugin-core-v1 | `6b3fdebeffdb` | 7 |
| Cyrene-Plugins-Official | | refactor/astrbot-capability-completion | `a6a2f6eec32d` | 21 |
| Cyrene-Reactor | develop `a056148` | chore/ci-authority-boundary-20260910 | `4322875228e7` | 1 |
| Cyrene-Reactor | | refactor/remove-hybrid-fallback-20260909 | `b444da487e03` | 1 |
| Cyrene-Reactor | | fix/remove-simulated-lora-swapper-20260909 | `d4eadc29f065` | 2 |
| Cyrene-Reactor | | refactor/remove-dead-sidecar-telemetry-20260909 | `ce117b633578` | 2 |
| Cyrene-Reactor | | refactor/remove-fallback-scheduler-bridge-20260909 | `29a4faaa5fd3` | 1 |
| Cyrene-Yield | develop `6931c2d9` | chore/ci-authority-boundary-20260910 | `2f2c86afd52c` | 1 |
| Cyrene-Exchange | develop `2bc5dfa` | chore/ci-authority-boundary-20260910 | `3734fb7371b6` | 1 |
| Cyrene-Exchange | | refactor/extract-spring-plugin-services-v1 | `4f8ea210a1af` | 5 |
| Cyrene-Catalyst | develop `35b7d3e` | chore/ci-authority-boundary-20260910 | `e64394a8a2cb` | 1 |
| Cyrene-Echo | develop `128b051` | chore/ci-authority-boundary-20260910 | `622667507657` | 1 |
| Cyrene-Navigator | develop `f83a701` | chore/ci-authority-boundary-20260910 | `c74d5a9d6925` | 1 |
| Cyrene-Workspace | main `950dc4b` | chore/remove-retired-workspace-config | `f72d3ac9a891` | 5 |
| Cyrene-Workspace | | chore/ci-authority-boundary-20260910 | `d539d51cc930` | 1 |
| Cyrene-Workspace | | chore/text-lifecycle-phase0-20260910 | `fbc5701bda8e` | 1 |
| Cyrene-Workspace | | fix/database-authority-truth-20260909 | `06aa5f769cfa` | 1 |
| Cyrene-Workspace | | fix/dh-integration-authority-20260909 | `78f3bd2edcb4` | 1 |

`chore/ci-authority-boundary-20260910` 在每个仓库中各有一个分支（共 9 个），它们**不是**跨仓库共享的同一个分支对象；应合并各仓库自己所拥有的副本。

计划编写时，所有 A 组合并都使用 `git merge-tree --write-tree --name-only origin/<base> <branch>` 进行了冲突 dry-run，结果均为 **CLEAN**。如果后续分支涉及相同文件，每次合并后都要重新检查冲突。

## 3. B 组 — 有冲突 / 尽力处理（可以未完成）

Dry-run 显示存在冲突；**不得盲目合并**。先判断“是否已被 develop 更新所取代”。

| 仓库 | 分支 | ahead / 冲突 |
|---|---|---:|
| Cyrene-Plugins-Official | chore/plugins-ci-boundary-20260910 | 12 / 166 |
| Cyrene-Plugins-Official | feat/unified-official-plugins | 9 / 141 |
| Cyrene-Platform | feat/training-capability-client-v1 | 1 / 10 |
| Cyrene-Yield | ci/azure-ci-baseline | 7 / 5 |
| Cyrene-Navigator | docs/navigator-bilingual-documentation | 1 / 5 |

可能的解决路径：`git checkout <base> && git merge <branch>`；如果没有冲突且 `git diff --diff-filter=U --quiet` 成功，则提交；否则运行 `git merge --abort` 并记录为“manual”。

## 4. C 组 — 不得触碰

- `Cyrene-Yield` 的 `archive/icy-lunar-llamafactory`（ahead 3090，是归档分支）。
- 所有 `dependabot/*` 分支（共 5 个），通过 GitHub PR 审查处理。
- 各仓库的 `main` 分支和零散本地 `origin` 分支（release / 意外重复分支）。
- 过期的 2026-08-26 至 2026-08-31 分支（例如 Yield `feat/artifact-plane-mvp-current`、日期为 08-31 的 `docs/*-bilingual-documentation`）。

## 5. 推送、验证与提升

1. **推送**（只有在所有 A 组本地合并完成后）：对每个发生变更的仓库运行 `git -C <repo> push origin <base>`。
2. **Azure**：确认每个仓库都有一次运行。必要时手动触发：`az pipelines run --name <repo> --branch develop --project Cyrene --organization https://dev.azure.com/dohorizon`。然后按需使用 `az pipelines runs list --pipeline-ids <id> ...` / `gh` 轮询。
3. 全部通过后，为每个仓库创建一个 **`develop → main` PR**（`gh pr create --base main --head develop ...`）。
4. **停止。**不要删除分支（由 owner 负责清理）。

## 6. 进度表（每步完成后更新）

| 仓库 | A 组合并 | 已推送 | Azure | dev→main PR |
|---|:---:|:---:|:---:|:---:|
| Cyrene-Platform | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Plugins-Official | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Reactor | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Yield | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Exchange | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Catalyst | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Echo | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Navigator | [ ] | [ ] | [ ] | [ ] |
| Cyrene-Workspace | [ ] | [ ] | [ ] | [ ] |

## 7. 给下一位 Agent 的恢复说明

- 如果在某仓库合并过程中中断，运行 `git -C <repo> status`。若正在合并，应完成合并或运行 `git merge --abort`，然后继续剩余 A 组分支。
- 合并前执行幂等检查：`git -C <repo> merge-base --is-ancestor <branch> origin/<base>`。若结果为 true，说明该分支已合入，应跳过。
- 绝不运行 `push --force`。绝不删除分支。绝不触碰 C 组。
