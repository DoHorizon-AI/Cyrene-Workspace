# CI Authority and Boundary / CI 权威与边界

This record is the current CI topology for the Cyrene repositories. It separates automatic source validation from delivery, private-credential, GPU, and exact multi-repository acceptance.

本记录定义 Cyrene 各仓库当前的 CI 拓扑，将自动源码验证与部署、私有凭据、GPU、精确多仓验收分开。

## Rules / 规则

1. Each repository has one automatic source and contract authority. The same source gate is not run automatically by both GitHub Actions and Azure Pipelines.
2. Public repository source and contract checks use GitHub Actions. Private repository source and package checks use Azure Pipelines.
3. Azure owns delivery, deployment, self-hosted GPU, private-credential, and exact multi-repository acceptance. These are supplemental scopes for public repositories and the required scopes for private repositories.
4. When Azure validates a GitHub-hosted repository, its result must be published as a check for the same commit. A successful Azure run does not establish merge, ancestry, canonical read-back, or hardware acceptance by itself.
5. Skipped, simulated, credential-blocked, and unrun work keeps its original state in evidence.

1. 每个仓库只有一个自动源码与契约验证权威；同一源码门禁不由 GitHub Actions 与 Azure Pipelines 自动重复执行。
2. 公开仓库的源码与契约检查使用 GitHub Actions；私有仓库的源码与包检查使用 Azure Pipelines。
3. 部署、自托管 GPU、私有凭据与精确多仓验收由 Azure 负责。对公开仓库这是补充范围，对私有仓库这是必需范围。
4. Azure 验证 GitHub 仓库时，结果必须以同一 commit 的 check 回写 GitHub。Azure 成功本身不代表 merge、祖先关系、canonical 回读或硬件验收已经完成。
5. skipped、simulated、凭据阻塞和未运行工作在证据中保留原始状态。

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
| `Cyrene-Workspace` | meta-workspace | Azure Pipelines | Exact multi-repository and full lifecycle acceptance | Manual product-contract fallback |

The authoritative per-repository declaration is `repositories.yaml`. Its `ci_authority` block records the automatic source provider, the delivery provider, and the Azure scope kept outside the source gate.

各仓库的权威声明位于 `repositories.yaml`。其中的 `ci_authority` 记录自动源码 provider、交付 provider，以及不并入源码门禁的 Azure 范围。

## Current cleanup / 当前清理

The Plugins Azure pipeline now classifies the diff before starting source jobs. Plugin, provider, connector, dependency, contract, test, and CI changes select their relevant gates; lockfiles, pipeline definitions, contracts, SDKs, tests, and CI tooling force all source gates. Its GitHub workflows are manual-only fallbacks.

Plugins 的 Azure pipeline 会先分类 diff，再启动源码 job。插件、provider、connector、依赖、契约、测试和 CI 变更只选择对应门禁；lockfile、pipeline 定义、契约、SDK、测试和 CI tooling 变更强制执行全部源码门禁。Plugins 的 GitHub workflow 仅保留手工回退。

The automatic Azure source triggers for the public Platform and Product repositories are disabled. Their GitHub source workflows remain the merge authority. Azure definitions remain available for the manual GPU, deployment, private-credential, or integration scopes they contain. The Workspace product-contract workflow and the no-op Catalyst/Echo CI workflows are also manual-only fallbacks.

公开 Platform 与 Product 仓库的 Azure 自动源码触发已关闭，GitHub 源码 workflow 继续作为 merge 权威。Azure definition 仍保留其中的手工 GPU、部署、私有凭据或集成范围。Workspace product-contract workflow 以及 Catalyst/Echo 的空操作 CI workflow 也改为仅手工回退。

The Azure service connection, pipeline definition, branch policy, and GitHub status check are external operational settings. They still require live verification after each repository's configuration is promoted; this repository change does not claim that external status wiring is already accepted.

Azure service connection、pipeline definition、分支策略和 GitHub status check 属于仓库外运维配置。各仓库配置提升后仍需实时验证；本次仓库变更不宣称这些外部状态回写已经验收。

## Evidence boundary / 证据边界

Local tests establish local evidence. Hosted Azure establishes the result for the exact Azure run and commit. A cross-repository run establishes only the checked-out revisions and exercised lane. Merge, remote ancestry, canonical read-back, production deployment, and real GPU/provider execution remain separate claims.

本地测试只建立本地证据；Hosted Azure 只建立具体 Azure run 与 commit 的结果；多仓运行只建立该次 checkout 的 revision 与实际执行 lane。merge、远端祖先关系、canonical 回读、生产部署以及真实 GPU/provider 执行仍是独立结论。
