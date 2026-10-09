# Task lifecycle and delivery cleanup

These requirements apply to every Cyrene repository and development tool. The repository-local `AGENTS.md` contains the common rules; `CLAUDE.md` and `CODEBUDDY.md` import that file. Codex, Cursor, and Antigravity can discover `AGENTS.md`. Tool prompts guide behavior; Git checks and server settings provide the executable safeguards.

## Start and deliver

Read `repositories.yaml`, the repository contribution guide, live refs, registered worktrees, open PRs, and working-tree state. Assign an owner and task branch. Use an ordinary branch when safe; create an isolated worktree when concurrent writers or dirty overlap require it. Consumers integrate exact remote commits rather than another contributor's mutable checkout.

Run the appropriate checks, normally merge the PR into the integration branch, fetch the remote, and verify that the merge is present. Preserve other contributors' commits and uncommitted work. Do not force-push, bypass branch protection, or reset a canonical checkout with unique commits.

## Complete and clean up

After the remote read-back, run the guarded cleanup helper from a directory outside any worktree it may remove:

```bash
python3 scripts/task_hygiene.py plan \
  --repo /absolute/path/to/repository --target develop \
  --only-branch codex/example-task \
  --output /outside/worktree/task-cleanup-plan.json

python3 scripts/task_hygiene.py apply \
  --planfile /outside/worktree/task-cleanup-plan.json \
  --apply-report /outside/worktree/task-cleanup-result.json
```

Omit `--only-branch` only for an authorized repository-wide housekeeping pass. Multiple `--repo` arguments permit one multi-repository plan. The helper uses Git and GitHub evidence rather than creating a second task registry or scheduler. Existing `agent-worktree.ps1` ownership and snapshot semantics remain applicable.

Cleanup requires either exact branch-tip ancestry or an exact-head merged PR into the expected integration branch whose merge commit is present. A reused branch with newer commits does not inherit an older PR's completion. Open PR heads, protected/default/integration/release branches, primary worktrees, locked or active worktrees, uncommitted/untracked work, and unmerged commits remain protected. Remote access or PR evidence failures must not become permission to delete.

When a merged task still has acceptance or trial work, or its owner has active follow-up work in the worktree, the owner must mark it with `git worktree lock --reason '<task/owner and ongoing verification>' <worktree-path>`. After the owner finishes the work and archives its evidence, the owner explicitly runs `git worktree unlock <worktree-path>` before a branch-scoped cleanup plan and apply. Cleanup helpers or other tools must never automatically unlock another owner's worktree.

The helper records recoverable Git tips under `refs/task-archive/`, preserves non-cache ignored data outside the worktree or refuses removal, and uses normal guarded Git worktree removal. Exact-SHA leases are permitted only for deleting an already verified task ref; branch history is never rewritten. A stale plan must be revalidated, and changing tips or new work cause a skip.

Do not recursively erase parent task directories. They may hold runtime configuration, trial services, acceptance reports, credentials, or environments that are outside the registered Git worktree. Do not run broad recursive deletions or `git clean -fdx`. Managed Codex app worktrees must use their owning app's archive mechanism where applicable.

## Status and unfinished work

```bash
python3 scripts/task_hygiene.py status --repo /absolute/path/to/repository
```

The default recent-work window is 14 days. Age helps identify stale unmerged work for its owner; it is never sufficient deletion evidence. A paused or unfinished task remains visible with its branch, tip, PR, owner when known, and preservation reason. A merged worktree still hosting a trial is retained as running, not mistaken for unfinished implementation.

The delivery report must include PR/merge SHA, the cleanup receipt, and every retained branch/worktree with its reason. Do not call cleanup successful while leaving unexplained temporary state. If this helper is unavailable, use native Git with the same preconditions and record the limitation.

## GitHub repository setting

Keep `delete_branch_on_merge=true` enabled on canonical repositories so successful PR merges remove their remote task branches automatically. This setting cannot clean a contributor's local worktree or local branches; the delivery helper remains necessary. Preserve `develop`, `main`, `release`, open promotion PRs, and other protected refs.

## Tool entry points

Keep thin versioned entry points in each repository rather than independent copies of tool-specific lifecycle rules:

- `AGENTS.md`: common mandatory delivery requirements.
- `CLAUDE.md`: imports `@AGENTS.md` for Claude.
- `CODEBUDDY.md`: imports `@AGENTS.md` for CodeBuddy.

References: [Antigravity directory rules](https://www.antigravity.google/docs/rules/), [Cursor project rules](https://cursor.com/docs/rules), and [CodeBuddy project instructions/imports](https://www.codebuddy.ai/docs/cli/memory).

---
<!-- Chinese Translation / 中文翻译 -->

## 中文要求

所有 Cyrene 仓库和开发工具都遵守同一交付要求。各仓库的 `AGENTS.md` 放共用规则；`CLAUDE.md`、`CODEBUDDY.md` 只导入它。规则用于让 agent 执行收尾，真正的删除约束由脚本中的 Git 检查和 GitHub 设置落实。

开工先查看拓扑、远端分支、worktree、开放 PR 与未提交改动。没有并行写入或脏文件冲突时可以使用普通任务分支，不强制每次创建 worktree。交付须通过对应检查、正常合并、拉取远端并核对合并结果。

合并后，从待清理 worktree 之外运行上面的 `plan`、`apply` 命令，通常用 `--only-branch` 限定本任务。脚本只根据 Git/GitHub 的真实证据清理，不创建另一套任务注册表。完整清理须留有可恢复的提交记录、外置私有资料备份或明确拒绝原因。

主工作区、受保护/主干/发布分支、开放 PR、未合并提交、未提交文件、被锁定或仍在运行的 worktree 都保留。分支名复用后产生的新提交不能沿用旧 PR 的“已完成”结论。14 天只是识别旧任务的默认时间窗口，不能据此删除未完成工作。

已合并任务仍在验收或试用，或 owner 仍在该 worktree 中进行后续工作时，应由 owner 执行 `git worktree lock --reason '<任务/owner 与持续验证原因>' <worktree-path>` 标记为活跃。owner 完成工作并归档证据后，显式执行 `git worktree unlock <worktree-path>`，再运行限定分支的清理计划与应用。清理脚本或其他工具不得自动解锁他人的 worktree。

禁止递归删除整个父任务目录或使用 `git clean -fdx`：同目录可能有试用服务、环境、验收报告、凭据和其他仓库。仍在提供试用服务的已合并工作区注明“运行占用”。受应用管理的 Codex worktree 使用其应用归档机制。

交付报告须列出 PR/合并 SHA、清理结果和每项保留原因；只有完成收尾或说明保留项才算交付完成。GitHub 的 `delete_branch_on_merge=true` 自动清理云端已合并任务分支，本机 worktree 和本地分支仍须由脚本收尾。
