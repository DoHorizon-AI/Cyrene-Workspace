# CI Authority and Boundary / CI 权威与边界

This record is the current CI topology for the Cyrene repositories. It separates automatic source validation from delivery, private-credential, GPU, and exact multi-repository acceptance.

本记录定义 Cyrene 各仓库当前的 CI 拓扑，将自动源码验证与部署、私有凭据、GPU、精确多仓验收分开。

## Rules / 规则

1. Each repository has one automatic source and contract authority. The same source gate is not run automatically by both GitHub Actions and Azure Pipelines.
2. Each repository declares one automatic source authority. GitHub Actions is the publication-target source authority; repository visibility alone does not select the provider.
3. Public repository source and contract checks use GitHub Actions. Private repository source and package checks use Azure Pipelines.
4. For public repositories, a successful GitHub Actions push run builds the release payload exactly once and publishes an immutable artifact named with the repository slug and commit SHA. Its manifest records the repository, workflow run, commit, artifact name, and SHA-256 payload digests.
5. Azure CD consumes only the explicitly selected GitHub Actions run artifact. It verifies the run's repository, completed/successful conclusion, head SHA, artifact identity, and manifest digests before promotion. Azure CD performs no checkout, build, or test.
6. Azure also owns self-hosted GPU, private-credential, and exact multi-repository acceptance. These remain supplemental scopes for public repositories and required scopes for private repositories; they are not source CI.
7. When Azure reports a result for a GitHub-hosted repository, its result must be published as a check for the same commit. A successful Azure run does not establish merge, ancestry, canonical read-back, or hardware acceptance by itself.
8. Skipped, simulated, credential-blocked, and unrun work keeps its original state in evidence.

1. 每个仓库只有一个自动源码与契约验证权威；同一源码门禁不由 GitHub Actions 与 Azure Pipelines 自动重复执行。
2. 每个仓库都声明一个自动源码权威；GitHub Actions 是发布目标的源码权威，不能仅凭仓库可见性推断 provider。
3. 公开仓库的源码与契约检查使用 GitHub Actions；私有仓库的源码与包检查使用 Azure Pipelines。
4. 对公开仓库，GitHub Actions 成功的 push run 只构建一次发布载荷，并发布以仓库 slug 和 commit SHA 命名的不可变制品。制品 manifest 记录仓库、workflow run、commit、制品名和载荷 SHA-256 摘要。
5. Azure CD 只消费显式选定的 GitHub Actions run 制品。提升前必须校验 run 的仓库、已完成且成功的结论、head SHA、制品身份与 manifest 摘要；Azure CD 不 checkout、不 build、不 test。
6. 自托管 GPU、私有凭据和精确多仓验收也由 Azure 负责。对公开仓库这是补充范围，对私有仓库这是必需范围；它们不属于源码 CI。
7. Azure 为 GitHub 仓库回报结果时，必须以同一 commit 的 check 回写 GitHub。Azure 成功本身不代表 merge、祖先关系、canonical 回读或硬件验收已经完成。
8. skipped、simulated、凭据阻塞和未运行工作在证据中保留原始状态。

## Repository matrix / 仓库矩阵

| Repository / 仓库 | Visibility / 可见性 | Automatic source authority / 自动源码权威 | Azure scope / Azure 范围 | GitHub role / GitHub 角色 |
| --- | --- | --- | --- | --- |
| `Cyrene-Platform` | public | GitHub Actions | Manual GPU/runtime and delivery acceptance | Required source, contract, and unit checks |
| `Cyrene-Plugins-Official` | private | Azure Pipelines | Required source, catalog, package, and private integration checks | Manual troubleshooting fallback |
| `Cyrene-Reactor` | public | GitHub Actions | Manual self-hosted CUDA/runtime and delivery acceptance | Required source and product checks |
| `Cyrene-Yield` | public | GitHub Actions | Manual self-hosted CUDA/runtime and delivery acceptance | Required source and product checks |
| `Cyrene-Exchange` | public | GitHub Actions | Manual private integration and delivery validation | Required source and product checks |
| `Cyrene-Catalyst` | public | GitHub Actions | Manual private integration and delivery validation | Required source and product checks |
| `Cyrene-Echo` | public | GitHub Actions | Manual private integration and delivery validation | Required source and product checks |
| `Cyrene-Navigator` | public | GitHub Actions | Manual private integration and delivery validation | Required source, desktop, and product checks |
| `Cyrene-Workspace` | meta-workspace | GitHub Actions | Verified artifact handoff; exact multi-repository and full lifecycle acceptance remains supplemental Azure scope | Automatic product-contract and source bundle artifact |

The authoritative per-repository declaration is `repositories.yaml`. Its `ci_authority` block records the automatic source provider, the delivery provider, and the Azure scope kept outside the source gate.

A live GitHub recheck on 2026-09-12 reported the eight Platform/Product repositories as public and Cyrene-Plugins-Official as private. This task intentionally excludes Plugins.

各仓库的权威声明位于 `repositories.yaml`。其中的 `ci_authority` 记录自动源码 provider、交付 provider，以及不并入源码门禁的 Azure 范围。

## Current cleanup / 当前清理

The Plugins GitHub workflow is the automatic source authority and runs repository-local contract, catalog, SDK, implementation, and package gates without private checkouts. Its Azure pipeline is manual-only for supplemental protected-resource or delivery evidence.

Plugins 的 GitHub workflow 是自动源码权威，仅依赖本仓库执行契约、目录、SDK、实现和打包门禁；Azure pipeline 改为手工补充受保护资源或交付证据。

Automatic Azure source triggers are disabled in repositories whose source authority is GitHub Actions. Their GitHub source workflows remain the merge authority and their successful push runs publish immutable artifacts consumed by the CD definition. Azure definitions remain available for explicit artifact promotion/handoff and for manual GPU, deployment, protected-credential, or integration evidence in supplemental definitions. Workspace's exact multi-repository acceptance remains supplemental; its CD definition performs only verified artifact handoff.

公开 Platform 与 Product 仓库的 Azure 自动源码触发已关闭，GitHub 源码 workflow 继续作为 merge 权威。Azure definition 仍保留其中的手工 GPU、部署、私有凭据或集成范围。Workspace product-contract workflow 以及 Catalyst/Echo 的空操作 CI workflow 也改为仅手工回退。

The Azure service connection, pipeline definition, branch policy, and GitHub status check are external operational settings. They still require live verification after each repository's configuration is promoted; this repository change does not claim that external status wiring is already accepted.

Azure service connection、pipeline definition、分支策略和 GitHub status check 属于仓库外运维配置。各仓库配置提升后仍需实时验证；本次仓库变更不宣称这些外部状态回写已经验收。

## Evidence boundary / 证据边界

Local tests establish local evidence. Hosted Azure establishes the result for the exact Azure run and commit. A cross-repository run establishes only the checked-out revisions and exercised lane. Merge, remote ancestry, canonical read-back, production deployment, and real GPU/provider execution remain separate claims.

本地测试只建立本地证据；Hosted Azure 只建立具体 Azure run 与 commit 的结果；多仓运行只建立该次 checkout 的 revision 与实际执行 lane。merge、远端祖先关系、canonical 回读、生产部署以及真实 GPU/provider 执行仍是独立结论。
