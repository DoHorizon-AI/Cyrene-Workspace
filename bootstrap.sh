#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

PROFILE="${1:-full}"

echo "=================================================="
echo " CYRENE WORKSPACE BOOTSTRAP (Profile: ${PROFILE})"
echo "=================================================="

echo -e "\n[1/4] Verifying developer toolchains..."
command -v uv >/dev/null 2>&1 && echo "  [OK] uv: $(uv --version)" || echo "  [WARN] uv not found."
command -v cargo >/dev/null 2>&1 && echo "  [OK] Cargo: $(cargo --version)" || echo "  [WARN] Cargo not found."
command -v dotnet >/dev/null 2>&1 && echo "  [OK] .NET SDK: $(dotnet --version)" || echo "  [WARN] dotnet not found."

echo -e "\n[2/4] Synchronizing Python environments with uv..."
case "${PROFILE}" in
    full)
        uv sync --locked --directory "${SCRIPT_DIR}/../Cyrene-Platform"
        uv sync --locked --directory "${SCRIPT_DIR}/../plugins"
        uv sync --locked --directory "${SCRIPT_DIR}/../services/cyrene-reactor" --extra dev --extra pro
        uv sync --locked --directory "${SCRIPT_DIR}/../services/Cyrene-Yield" --extra dev
        uv sync --locked --directory "${SCRIPT_DIR}/../services/cyrene-exchange" --extra dev
        uv sync --locked --directory "${SCRIPT_DIR}/../services/cyrene-astrbot-rev/python/capability_worker"
        ;;
    astrbot)
        uv sync --locked --directory "${SCRIPT_DIR}/../Cyrene-Platform"
        uv sync --locked --directory "${SCRIPT_DIR}/../plugins"
        uv sync --locked --directory "${SCRIPT_DIR}/../services/cyrene-exchange" --extra dev
        uv sync --locked --directory "${SCRIPT_DIR}/../services/cyrene-astrbot-rev/python/capability_worker"
        ;;
    platform)
        uv sync --locked --directory "${SCRIPT_DIR}/../Cyrene-Platform"
        uv sync --locked --directory "${SCRIPT_DIR}/../plugins"
        ;;
    training)
        uv sync --locked --directory "${SCRIPT_DIR}/../Cyrene-Platform"
        uv sync --locked --directory "${SCRIPT_DIR}/../plugins"
        uv sync --locked --directory "${SCRIPT_DIR}/../services/Cyrene-Yield" --extra dev
        ;;
    *)
        echo "Unknown profile: ${PROFILE}. Valid profiles: full, astrbot, platform, training."
        exit 1
        ;;
esac

echo -e "\n[3/4] Restoring .NET solution(s)..."
if [ "${PROFILE}" = "astrbot" ]; then
    dotnet restore "${SCRIPT_DIR}/solutions/Cyrene.AstrBot.Integration.slnx"
elif [ "${PROFILE}" = "full" ]; then
    dotnet restore "${SCRIPT_DIR}/Cyrene.Workspace.slnx"
fi

echo -e "\n[4/4] Bootstrap complete for profile '${PROFILE}'!"
