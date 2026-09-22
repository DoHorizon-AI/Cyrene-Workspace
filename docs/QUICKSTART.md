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
