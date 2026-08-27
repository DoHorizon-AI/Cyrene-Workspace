#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "=================================================="
echo " CYRENE WORKSPACE BOOTSTRAP (POSIX)"
echo "=================================================="

# 1. Toolchain Verification
echo -e "\n[1/5] Verifying developer toolchains..."
command -v dotnet >/dev/null 2>&1 && echo "  [OK] .NET SDK: $(dotnet --version)" || echo "  [WARN] .NET SDK not found."
command -v cargo >/dev/null 2>&1 && echo "  [OK] Rust / Cargo: $(cargo --version)" || echo "  [WARN] Cargo not found."
command -v uv >/dev/null 2>&1 && echo "  [OK] uv: $(uv --version)" || echo "  [WARN] uv not found."

# 2. Topology Verification
echo -e "\n[2/5] Checking repository topology..."
REPOS=(
    "Cyrene-Platform:../Cyrene-Platform:https://github.com/DoHorizon-AI/Cyrene-Platform.git"
    "Cyrene-Plugins:../plugins:https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git"
    "cyrene-astrbot-rev:../services/cyrene-astrbot-rev:https://github.com/DoHorizon-AI/Astrbot-Rev.git"
    "cyrene-dh-system-internal:../services/cyrene-dh-system-internal:https://dohorizon@dev.azure.com/dohorizon/Cyrene/_git/DH-System-Internal"
    "cyrene-reactor:../services/cyrene-reactor:https://github.com/DoHorizon-AI/Cyrene-Reactor.git"
    "Cyrene-Yield:../services/Cyrene-Yield:https://github.com/DoHorizon-AI/Cyrene-Yield.git"
    "cyrene-exchange:../services/cyrene-exchange:https://github.com/DoHorizon-AI/Cyrene-Exchange.git"
    "cyrene-catalyst:../services/cyrene-catalyst:https://github.com/DoHorizon-AI/Cyrene-Catalyst.git"
    "cyrene-echo:../services/cyrene-echo:https://github.com/DoHorizon-AI/Cyrene-Echo.git"
    "cyrene-navigator:../services/cyrene-navigator:https://github.com/DoHorizon-AI/Cyrene-Navigator.git"
)

for entry in "${REPOS[@]}"; do
    IFS=":" read -r name path remote <<< "${entry}"
    if [ -d "${path}" ]; then
        echo "  [FOUND] ${name} at ${path}"
    else
        echo "  [MISSING] ${name} at ${path}"
        if [ -n "${remote}" ]; then
            echo "    Cloning ${remote}..."
            git clone "${remote}" "${path}"
        fi
    fi
done

# 3. Python Environment Sync
echo -e "\n[3/5] Synchronizing Python environments with uv..."
[ -f "../Cyrene-Platform/pyproject.toml" ] && uv sync --directory "../Cyrene-Platform"
[ -f "../plugins/pyproject.toml" ] && uv sync --directory "../plugins"
[ -f "../services/cyrene-reactor/pyproject.toml" ] && uv sync --directory "../services/cyrene-reactor" --extra dev --extra pro
[ -f "../services/Cyrene-Yield/pyproject.toml" ] && uv sync --directory "../services/Cyrene-Yield" --extra dev
[ -f "../services/cyrene-exchange/pyproject.toml" ] && uv sync --directory "../services/cyrene-exchange" --extra dev
[ -f "../services/cyrene-astrbot-rev/python/capability_worker/pyproject.toml" ] && uv sync --directory "../services/cyrene-astrbot-rev/python/capability_worker"

# 4. .NET Restore
echo -e "\n[4/5] Restoring .NET meta-solution..."
if command -v dotnet >/dev/null 2>&1 && [ -f "Cyrene.Workspace.slnx" ]; then
    dotnet restore Cyrene.Workspace.slnx
    echo "  [OK] Cyrene.Workspace.slnx restored."
fi

echo -e "\n[5/5] Bootstrap complete!"
