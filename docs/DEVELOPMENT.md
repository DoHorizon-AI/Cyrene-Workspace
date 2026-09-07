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
