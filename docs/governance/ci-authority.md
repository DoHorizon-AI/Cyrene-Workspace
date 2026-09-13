# CI/CD and branch authority / CI/CD 与分支权威

This policy applies to all nine canonical repositories: Workspace, Platform,
Plugins, Reactor, Yield, Exchange, Catalyst, Echo, and Navigator.

本策略适用于九个规范仓库：Workspace、Platform、Plugins、Reactor、Yield、
Exchange、Catalyst、Echo 与 Navigator。

## Provider authority / Provider 权威

1. GitHub Actions is the only repository CI authority. It performs source,
   contract, test, build, package, and immutable-artifact production.
2. Azure Pipelines is CD only. It has no automatic trigger, performs no source
   checkout, build, or test, and accepts only an explicitly selected successful
   GitHub Actions run and its exact-SHA artifact.
3. Azure verifies repository identity, run conclusion, commit SHA, artifact
   identity, manifest content, and payload digests before deployment or handoff.
4. A skipped, simulated, blocked, or unrun check is never reported as passed.

1. GitHub Actions 是唯一的仓库 CI 权威，负责源码、契约、测试、构建、打包以及
   不可变制品生成。
2. Azure Pipelines 仅负责 CD：无自动触发、不 checkout 源码、不构建、不测试；
   只消费显式指定且成功的 GitHub Actions run 及其精确 SHA 制品。
3. Azure 在部署或交接前必须校验仓库身份、run 结论、commit SHA、制品身份、
   manifest 内容与载荷摘要。
4. skipped、simulated、blocked 或未运行检查不得记为通过。

## Branch lifecycle / 分支生命周期

- `develop` is the default development branch. It intentionally has no GitHub
  branch protection or ruleset, so maintainers may merge or push normally.
  GitHub Actions still runs on every `develop` change.
- `main` is protected. It accepts promotion pull requests only from
  `develop`, and the repository's required GitHub CI checks must be green.
- `release` is protected. It accepts promotion pull requests only from
  `main`, after the exact `main` revision has produced a buildable,
  immutable GitHub artifact.
- Force pushes and branch deletion are disabled for `main` and `release`.
  Protection is not bypassed for routine promotion.

- `develop` 是默认开发分支，并且有意不设置 GitHub branch protection 或
  ruleset，维护者可以正常合并或推送；每次变更仍会运行 GitHub Actions。
- `main` 受保护，只接受 `develop` 发起的提升 PR，且仓库要求的 GitHub CI
  必须全绿。
- `release` 受保护，只接受 `main` 发起的提升 PR；对应的精确 `main` revision
  必须已经生成可构建、不可变的 GitHub 制品。
- `main` 与 `release` 禁止 force push 和删除；日常提升不得绕过保护。

## Promotion evidence / 提升证据

For each promotion, record the source and target SHA, required check conclusions,
normal PR merge, remote fetch/read-back, and ancestry. Azure success proves only
the selected CD run; it does not replace GitHub CI or prove a merge.

每次提升都应记录源/目标 SHA、required checks 结论、正常 PR 合并、远端回读与祖先
关系。Azure 成功只证明该次 CD run，不能替代 GitHub CI，也不能证明已经完成合并。
